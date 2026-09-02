# Dream v2 — global scope, resume cursor, unified decay

**Date**: 2026-08-08
**Status**: design — **passed adversarial review on 2026-08-08 at 03:24 UTC; figures replayed,
corrections logged in §11.** An operator decision is pending and blocks step 6a:
§4.3, property 6.
**Supersedes the scope** of
`2026-08-08-dream-project-pool-design.md` (hereafter "v1"), inherits its inventory of
couplings, and makes executable six decisions the operator made after reading it.
**Scope**: `brain_v42` only — `scripts/dream.sh`, the phases, `dream_runs`, the decay
computation and its two notions of freshness, the systemd unit and its timer.
**Worktree**: `.claude/worktrees/dream-pool`, branch `feat/dream-project-pool`.
**Migration measured in production at time of writing**: `041`

```
docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"
→ 041
```

**No implementation code accompanies this spec.** `scripts/dream.sh` runs from the **root**
working tree, every day. Everything this batch adds lives under `docs/`.

---

## 0. How to read the numbers in this spec

Three qualities of number, never mixed:

- **Measured** — a command was run today, cited next to the result.
- **Inherited from v1** — measured on 2026-08-08 by the previous spec and re-cited, not
  re-measured. Flagged with "v1 §N".
- **Arithmetic** — a deduction from constants read in the code. Not a runtime
  measurement, and stated as such every time.

**The methodological caveat from §4 and §5 is lifted.** The first draft published
multipliers derived from a **SQL replica** of `src/brain_v42/services/decay.py`, without
having run the Python. The adversarial review replayed the computation by importing the
**real** `DecayCalculator`, read-only, over the 2,343 learnings that are neither archived nor
merged, exported via `COPY … TO STDOUT`:

```
PYTHONPATH=<worktree>/src .venv/bin/python  →  from brain_v42.services.decay import DecayCalculator
```

The replica and the real code **agree**: mean 0.5130 / 0.4493, minimum 0.2228, `stale`
1,094 / 1,453, `archived` 0 / 0, `< 0.25` = 49, `< 0.30` = 236. Only the p10 of the human
counter differs by 0.0001 (0.2905 vs. the published 0.2906), an interpolation-method gap. The
figures in §4 and §5 are therefore **measured in the strong sense**. The rule "one computation
in the repo" (§4.3, property 1) still stands in full: it targets the implementation, not the
measurement.

**An adversarial review replayed the entire spec on 2026-08-08 at 03:24 UTC.** What changed is
logged in §11, including one figure that was wrong.

---

## 1. What v1 contributes, and what it loses

v1 designed a **hardcoded pool of eight projects**. After reading it, the operator ruled
otherwise: **the whole brain is eligible** (D1). Superseding must be explicit, because v1
remains a useful document and half its content does not change.

### 1.1 What falls away

| v1 | what falls away | why |
|---|---|---|
| §1 — the pool of eight hardcoded keys | **falls away entirely** | D1: no more hardcoded list |
| §2 — "sub-project exclusion and its cost" (479 artifacts) | **falls away as debt** | the six two-tier keys are eligible like the others; there is no more exclusion to pay for |
| §4.2 — the "8 × agent + 1 × global" simulation (80 min, p90 126.6, max 286.3) | **falls away as sizing** | we no longer multiply by a fixed cardinal: we fill a window (§7). The **method** — break down night by night, take the p90, never multiply averages — stays and is reused |
| §4.3 — the configured ceiling "8 × 53 min + 35" (459/803 min) | **falls away** | at 55 projects this computation gives **2,950 min** (55 × 53 + 35; `PHASES`, `dream.sh:106-113` → 5+5+8+15+10+10) — the first draft said 3,050, off by 100 min. The ceiling stops being a useful bound and becomes a window (§7) |
| §6 — `BRAIN_DREAM_PROJECT_POOL` as an **inclusion list** | **falls away** | there is no more inclusion to declare. The two transport pitfalls §6 measured **remain valid** and apply as-is to any future list (exclusion, opening canary) |
| §12 step 6 — "open the pool one key at a time" | **changes shape** | we no longer open keys, we open a **window**; progressive opening happens by window width (§7, §8) |
| §16 — "the pool covers 62.4% of the corpus" | **falls away** | the target coverage is 100%, spread over several nights |

### 1.2 What stays, and is not up for debate again

All of this is measured by v1, re-cited, not re-measured unless stated otherwise.

- **The lock is global** — `scripts/dream.sh:351`,
  `LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/brain-v42-dream.lock"` + `flock -n 9`. Several systemd
  units are structurally impossible: any further ones exit green doing nothing.
  **One unit, one loop.** At 55 projects this argument does not weaken, it strengthens.
- **Seven of twelve log paths gain a project component**, five do not
  (v1 §3.2). The uniform rule "project every path" produces wrong code in both
  directions. Unchanged — except that the truncation in `codex_runner.py:419,432-433` now
  hits up to 25 projects a night instead of 8.
- **The loop is project-major**, never phase-major (v1 §3.3): `PHASE_DEPS`
  (`dream.sh:116-123`) reinjects the previous phase's report **by re-reading a file**
  (`:205-226`).
- **Two exports survive across iterations** and must be reset to `[]` at the start of each
  project iteration (v1 §3.4): `PROMOTE_CANDIDATE_POOL_JSON`,
  `PROMOTE_RECENT_PROMOTIONS_JSON` (`dream.sh:504-510`, re-read at `:196-197`).
- **Four prompt lines hardcode `brain-v42`**, and it's the batch's only irreversible defect
  (v1 §3.5): `phase_synth.md:24,58`, `phase_promote.md:4`, `phase_connect.md:43`. Plus
  `promote_validate.py:67-179`, which does **not** check `project_key`. At 55 projects, SYNTH
  would write insights tagged `brain-v42` for everyone. **These lines fall before the
  loop, not with it.**
- **`extract`, `roadmap` and `sweep` are global** and receive no `--project-key`
  (v1 §7). They sit outside the loop and run once. `roadmap` keeps its own rotation
  (`roadmap_curate.py:437-441,474-487`), measured at 30 projects, which **is not** the domain of
  the loop and must never be read as coverage.
- **`sweep` is irreversible and stays out of scope** (v1 §8): shipped closed and dry, never
  armed in the same batch as a topology change. Re-measured today: `sweep`
  **still does not appear** in `dream_runs` —
  `SELECT phase, count(*) … GROUP BY phase` returns eight phases, without it.
- **Migration 042** (`dream_runs.project_key VARCHAR(64) NULL`, no backfill, sentinel
  `'*'` for global phases, index `(run_date DESC, project_key)`) **precedes the loop**
  (v1 §5, §12). Re-checked today: `\d dream_runs` shows only `dream_runs_pkey(id)` and
  `idx_dream_runs_date(run_date DESC)`, `phase` is `character varying(10)`, and
  `dream_promotions.dream_run_id` references it as `ON DELETE SET NULL`.
- **Full failure isolation between projects, no quorum, one aggregated alert**
  (v1 §9, §11). The counters become `project/phase` pairs. The five `continue` statements in
  the phase loop (`:447,455,469,501,522`) need to be requalified.
- **The server scope exists, is tested, and is off** (v1 §14.1). See §1.3: this is the fact
  that changes status.

### 1.3 The v1 fact that changes status: the disabled scope

v1 §4.1 measured that in five of the six phase prompts, `{{PROJECT_KEY}}` appears only in
prose, and that `brain_decay_status()` (`decay_tools.py:56`), `brain_consolidation_candidates`,
`brain_backfill_links_batch`, `brain_list_orphans_for_classification` and `brain_get_clusters`
carry an **empty** `DreamProjectToolPolicy()`. The middleware
(`services/dream_project_scope.py`, `mcp/dream_capabilities.py:224-261`) is inert:
`dream.sh:20` reads `BRAIN_DREAM_CAPABILITY_ENFORCEMENT="${…-false}"` and the variable is absent
from the `.env` as well as from the three drop-ins.

v1 made this an **open question**. At global scope it can no longer be one:

- at 8 projects, looping without scope cost 80 min for the result of 15 — a waste;
- at ~25 projects a night, that's **25 times the same global work**, and the loop produces
  no additional coverage. It only manufactures `dream_runs` rows and token
  consumption.

**Consequence named, not decided**: the full-window loop only makes sense after the
five empty policies are filled in. It **ships** before that (§8), closed to one project, because
all the infrastructure — cursor, logs, counters, alert — is independent of the scope.
It does not **open** before that. That's step 7 of §8.

And part of this spec does not wait for the scope at all: §4 and §5 work **per entity**,
not per run. They produce an effect from the very first night, on a single project.

---

## 2. The cursor: structure, persistence, resume, appearance, disappearance, starvation

### 2.1 What it must solve, measured

Iteration domain and mass, measured today:

```sql
WITH a AS (SELECT project_key FROM learnings UNION ALL SELECT project_key FROM decisions
           UNION ALL SELECT project_key FROM snippets UNION ALL SELECT project_key FROM runbooks
           UNION ALL SELECT project_key FROM adrs)
SELECT coalesce(project_key,'(null)'), count(*) FROM a GROUP BY 1 ORDER BY 2 DESC;
```

| fact | measured value |
|---|---|
| `project_contexts` | **55** (see §2.5: it was 54 thirty minutes ago) |
| artifacts (5 knowledge tables) | **3,803** |
| artifacts without `project_key` | **0** — see the box below |
| artifact keys **without** a `project_context` | **9** (`openclaw` 7, `red-backup` 4, `red-cli` 2, `red-feed`, `lyriks-backend-v2`, `red-dataset`, `red-life`, `hawkixs-infra`, `hk-anime-list` — 19 artifacts) |
| `project_contexts` **with no artifacts at all** | **9** (`red-daemon`, `red-llm`, `red-e2e-target`, `red-lab:developer-gemini`, `red-tsdb`, `red-api`, `red-lab:developer-opus`, `red-alerts`, `red-lab-factory`) |
| projects where dream has already synthesized | **1** — the same query on `learnings`, `snippets` and `decisions` returns **a single row, `learnings / brain-v42: 87`**. No snippet carries the tag, even though `phase_synth.md:30` makes it produce one |

> **The "26 artifacts without `project_key`" does not exist.** The first draft of this spec
> showed it in the "measured value" column and repeated it in §10. It comes from v1 §1
> ("26 of these artifacts carry `project_key IS NULL`") and was not replayed. Replayed:
>
> ```sql
> SELECT count(*) FILTER (WHERE project_key IS NULL) FROM learnings;  -- and the 4 others
> → learnings 0 · decisions 0 · snippets 0 · runbooks 0 · adrs 0 · indexed_plans 0
> ```
>
> Zero, across the five knowledge tables **and** `indexed_plans`. The only table in the schema
> that still carries null `project_key` values is `brain_entities` (**13** out of 4,970) and
> `search_log` (275 out of 1,351) — neither of which is a knowledge artifact. This is
> exactly the defect §0 claims to forbid: an inherited figure, republished under the label
> "measured". The iteration domain is unaffected; the lesson is not.

**The corpus moves while it is being measured.** Total mass went from **3,803 to 3,804** and
`brain-v42`'s from **658 to 659** between the start and end of this session (~40 min). Both
values appear in this spec depending on when they were measured; this is a real gap, not
a typo, and it carries the same lesson as §2.5.

**The iteration domain and the artifact domain do not coincide.** Nine contexts carry
nothing; nine keys carry artifacts without a context. A cursor that loops over
`project_contexts` will never see the 19 artifacts of the nine orphan keys; a cursor that
loops over artifact keys will never see the nine empty contexts. **This choice must be stated.**

**Decided**: the cursor iterates over `project_contexts`. Reason: it's the domain the phases
know how to address (`--project-key`, `brain_get_project`, the roadmap, the focus), and a key
without a context has no focus to update and no roadmap to curate. The 19 orphan artifacts
become a **binding signal** — the same family as the
`2026-07-27-orphan-project-key-bind-design.md` initiative — not a loop case. To be named in
the nightly report, not handled in the loop.

Durations, re-measured over the last 30 nights (v1 §4.2 method, night by night):

```sql
WITH n AS (SELECT run_date,
  sum(duration_s) FILTER (WHERE phase IN ('scan','clean','connect','synth','promote','reorg')) agent_s,
  sum(duration_s) FILTER (WHERE phase IN ('extract','roadmap','sweep')) global_s
 FROM dream_runs WHERE run_date >= (SELECT max(run_date) FROM dream_runs) - 29 GROUP BY 1)
SELECT avg(agent_s), percentile_cont(0.9) WITHIN GROUP (ORDER BY agent_s), max(agent_s), … FROM n;
```

| subtotal | mean | p90 | max |
|---|---|---|---|
| **agent** phases (the six in the loop) | **9.28 min** | **14.73 min** | **30.50 min** |
| **global** phases (extract, roadmap, sweep) | 5.77 min | — | **42.26 min** |

(The global max of 42.26 min exceeds the configured ceiling of 35 min across the three
`timeout`s: it includes manual re-runs, as v1 had already noted for its own series.)

### 2.2 Structure: one date per project, not a global pointer

**Decided: the cursor is a per-project state — a trial date — not an index into a
list.**

The argument is structural. The project list is **re-sorted every night** by human
priority (§3), which shifts on every read. A pointer saying "we stopped at #25" names a
different project from one night to the next without anything consolidated in between: it
guarantees **no** coverage. A per-project date is stable under insertion, under deletion and
under re-sorting — it's the only form for which "everything eventually gets covered" can be proven.

Proposed minimal shape:

```
dream_project_cursor(
  project_key      TEXT PRIMARY KEY,   -- no FK, cf. §2.6
  last_attempt_date DATE NOT NULL,     -- advanced on ADMISSION, not on success
  last_success_date DATE,              -- NULL = never succeeded
  attempts          INT  NOT NULL,
  consecutive_failures INT NOT NULL
)
```

`last_attempt_date` carries coverage. `last_success_date` carries health, and it's the
one that answers "has this project ever been consolidated?" — the question §6 makes a
precondition for purge.

### 2.3 Where it persists: its own table, neither `project_contexts` nor `dream_runs`

**Not in `project_contexts`.** Measured:

```
docker exec brain_v42_postgres psql -U brain -d brain -c "\d project_contexts"
→ trg_project_contexts_updated BEFORE UPDATE ON project_contexts
     FOR EACH ROW EXECUTE FUNCTION set_project_context_updated_at()
```

and the function body, read from `pg_proc`, rewrites `NEW.updated_at := CURRENT_TIMESTAMP`
on **every** UPDATE, except under the explicit setting
`brain_v42.allow_explicit_project_context_updated_at`. A cursor write every night for every
project would therefore make **all** projects look "updated tonight", destroying the
inactivity signal purge needs (§6). This is exactly the mistake 040 fixed
for the focus, about to be recommitted at the project scale. On top of that, `project_contexts`
carries **seven** triggers and a compare-and-swap on `focus_revision`: hooking a nightly writer
into it creates a concurrency surface with `brain_update_project_focus` for zero gain.

**Not derived from `dream_runs.project_key`** — and that's the temptation to explicitly rule
out, because 042 would make the derivation free ("the next project = the one whose
`max(run_date)` is oldest"). It fails on one precise case: the cursor would only advance
if a row is written. But v1 §5 had measured that three of the five INSERT sites in
`dream_runs` swallow their exception best-effort (`ticket_extract.py:752`,
`roadmap_curate.py:1146`, `maintenance/session_sweep.py:94`, on the model
`except Exception: print(f"! warning: …")`). **Re-measured on 2026-08-09, it's worse, and the
argument comes out stronger: all FIVE lose their trace silently** (§14.2). The fifth, `dream_parser` —
the one that carries all the per-project telemetry — does raise in Python, but its
orchestrator catches it: `dream.sh:338`, `WARN dream_parser failed for $name (non-fatal)`. The
only writer the derivation would have needed is therefore also silent on failure. A project
whose phases die before writing
anything at all would keep a `NULL` cursor, hence stay **at the head of the line, every night,
forever** — it would consume the start of every window and block rotation. This is
starvation, and it would be invisible.

**Non-negotiable rule that follows: the cursor advances on ADMISSION, never on success.**
`last_attempt_date` is written before the project's first phase, in its own transaction.
If this write fails, the project **is not admitted** — fail-closed, otherwise we reopen the
hole we just closed.

`dream_runs.project_key` (042) stays useful and stays shipped: it is **telemetry** (which
phase ran, for which project, with what status). The cursor is **scheduling**. Confusing
the two makes coverage depend on a table where three writers are
best-effort.

### 2.4 Resume

When the window opens, a single query, with no hidden state:

```
ORDER BY  last_attempt_date ASC NULLS FIRST,   -- 1. coverage
          human_priority     DESC,             -- 2. order within the band (§3)
          artifact_mass      DESC,             -- 3. stable tiebreak
          project_key        ASC               -- 4. total determinism
```

`run_date` and `last_attempt_date` are **dates**, not instants: all projects served
the same night end up in **the same tie band**. At ~25 projects a night, the band
is 25 elements wide — human priority actually orders something there, which would not be
the case with a second-level timestamp. **This is a property of the column type, not to be
"improved" into `TIMESTAMPTZ` without destroying the effect.**

Coverage then reads as arithmetic, not as a promise: each night serves the
N oldest; a served project moves to the back of the line; no project can be leapfrogged twice by
the same other one.

**This property was simulated, not just asserted.** The question the review had to
settle: *is a project with zero human priority ever reached, or is the head of the list
reordered ahead of it forever?* Over the 55 real projects — of which **44 have strictly
zero priority** and 9 zero mass — simulating the ordering above gives:

- **no project is ever left unserved**, over 60 nights, in the five configurations tested (§2.5);
- the worst project by sort key (`red-alerts`: priority 0, mass 0, last alphabetically in
  its band) is served on **night 7** in the most restrictive configuration (window 14,
  cap 7);
- the steady-state revisit interval is **bounded**: 3 nights at window 25, 4 nights at
  window 14, never more.

The reason is structural and outweighs the simulation: the primary key is a **date**, and
a served project gets today's date, hence the most recent of all. It cannot become
prioritized again over an unserved project. The sort is a strict FIFO on dates, and priority
only orders **within** an equivalence class. **D3's ratchet does not come back in through
the sort's door.**

### 2.5 A project appearing — case observed while writing this spec

At the start of this session, `SELECT count(*) FROM project_contexts` returned **54**. Thirty
minutes later, the same query returns **55**, and
`SELECT project_key, created_at FROM project_contexts ORDER BY created_at DESC LIMIT 3` returns
`perso | 2026-08-08 03:11:29+00`, two minutes before the measurement. **The domain moves under
the cursor; this isn't a hypothesis.**

Behavior: a new project has no cursor row, `NULLS FIRST` puts it at the head, it gets
served the following night. This is the right default behavior — a project that was just
created is the one being talked about.

**The risk is the batch, not the unit.** Measured creation rate:

```sql
SELECT date_trunc('month',created_at)::date, count(*) FROM project_contexts GROUP BY 1 ORDER BY 1;
→ jan 6 · fev 8 · mars 25 · avr 2 · mai 1 · juin 3 · juil 7 · aout 3 (8 days)
```

**March 2026: 25 contexts in one month.** A month like that fills an entire night with
never-served projects and pushes back everyone else's rotation by one turn. A batch of 25
creations on the same day fills it **exactly**.

**Decided**: cap the share of the window allocated to never-served projects. This isn't a
physical constant; it should be re-measured after a quarter. The cap is a **rotation floor**,
not an exclusion: the lower half of the window stays open to new arrivals the following
night.

**The first draft's numbers were wrong on three points, and the review measured them.**
It proposed "half the window, i.e. ~12 units out of ~25 on average", presented as
"the smallest cap that absorbs a March-sized month in two nights".

1. **Two nights, no.** 25 creations at 12 a night take **three** nights (12 + 12 + 1). The
   smallest cap that fits in two nights is **13**.
2. **~25, no either.** §7.3 explicitly refuses to plan on the average and uses the p90,
   i.e. **~14 projects a night**. Half the window the spec plans on is therefore
   **7**, not 12 — and 25 creations then take **four** nights.
3. **The cap bites hardest at cutover, and §7.3 ignored that.** On cutover night,
   **all 55 projects are "never served"**. The cap therefore applies to everyone, and
   there is no already-served project to fill the rest of the window with: the lower half
   runs empty.

Simulation of the §2.4 ordering over the **55 real projects**, with their measured
priorities and masses (human counter from §3.1):

| window | new-project cap | admitted nights 1-5 | full coverage | max revisit in steady state |
|---|---|---|---|---|
| 25 | none | 25 · 25 · 25 · 25 · 25 | **night 3** | 3 nights |
| 25 | 12 | **12** · 24 · 25 · 25 · 25 | **night 5** | 3 nights |
| 14 | none | 14 · 14 · 14 · 14 · 14 | **night 4** | 4 nights |
| 14 | **7** (half) | **7** · 14 · 14 · 14 · 14 | **night 8** | 4 nights |
| 14 | 12 | 12 · 14 · 14 · 14 · 14 | **night 5** | 4 nights |

**The cap doubles the delay to first full coverage** (8 nights instead of 4 at p90) and loses
half of the first night. The "2.5 / 4.0 / 8.2 nights" in §7.3 are computed **without**
it; with it, read the "full coverage" column above.

**Decided after measurement**: the cap applies **only from the point where there
already are served projects**, i.e. never during the first rotation. Formally, it
caps the share of new ones **relative to available candidates**, not in absolute terms:
`min(cap, window − nb_of_already_served_eligible_projects)` applies only if the second
term is positive. At steady state it is 7 over a window of 14; at cutover it
disappears. This is what makes the two paragraphs compatible rather than contradictory.

### 2.6 A project disappearing

A deleted `project_context` (§6) leaves an orphan cursor row.

**Decided: no foreign key between `dream_project_cursor.project_key` and
`project_contexts`.** Both available behaviors are bad:

- `ON DELETE RESTRICT` would turn the cursor into a **veto on purge** — the scheduling row
  would prevent deleting the project, which makes no sense and would be discovered at the worst moment;
- `ON DELETE CASCADE` would silently erase the **proof that this project was visited**, which
  is precisely the precondition §6 makes the entry ticket for purge. Purging a project
  would erase the trace that authorized purging it.

The nightly selection therefore does a `JOIN` on `project_contexts`: an orphan row is
**inert**, never selected, and never lost. The nightly report counts contextless
cursor rows — this is the only place where a disappeared project will be known.

Measured comparison to show this isn't theoretical:
`brain_sessions.project_key → project_contexts` is declared **`ON DELETE RESTRICT`**, and
`SELECT count(DISTINCT project_key), count(*) FROM brain_sessions` returns **17 projects, 363
sessions**. Seventeen of 55 projects are already indelible because of an FK written for another
reason. Adding a second FK in the same place without thinking it through would redo this
constraint blind.

### 2.7 Starvation — the four mechanisms, and the one that stays open

1. **Advance on admission** (§2.3). A project that crashes on the first phase has still
   consumed its turn. Without this, a broken project eats the head of the window every night.
2. **Daily tie band** (§2.4). Human priority can only reorder **within** a night, never jump
   a band. A very-read project cannot pass twice
   times before a project that has never been read.
3. **Never-served cap** (§2.5). A batch of creations does not suspend rotation.
4. **The slow project doesn't eat the next one's turn**: admission is decided on **time left**
   in the window (§7), not on a project counter. A project that overruns pushes back the
   admission frontier, it doesn't steal a turn from another one — the one that isn't admitted
   keeps its old cursor date and moves back **to the head** the following night.

**Stays open, but smaller than announced**: a project that overruns *systematically* (for
example a corpus that times out SYNTH every night) consumes a large share of the window each
night while still advancing its cursor. It blocks no one — but it reduces N for everyone,
repeatedly.

The first draft said "a disproportionate share" and "there is no data to calibrate a
per-project cap". **The review measured that the per-project cap already exists, hardcoded.**
`PHASES` (`dream.sh:106-113`) gives each phase its own `timeout Nm` (`:260`):
`5+5+8+15+10+10 = 53 min`, and the retry (`:539`, never on `promote`) adds `43 min` at worst.

```
structural cap for one project    = 53 min without retry, 96 min with
maximum share of the loop (205)   = 25.9% without retry, 46.8% with
```

A project therefore cannot exceed **47%** of the window no matter what, and it always
leaves enough for at least one other. What's missing isn't a cap, it's the
**empirical distribution**: no night with more than one project has ever run (v1 §9). The spec
therefore refuses to tighten this cap by guesswork and locks in the exit: `consecutive_failures`
and the gap `last_attempt_date − last_success_date` are **persisted from the first batch on**
so the question can be settled on measured data after a few nights. Setting a finer threshold
today would be exactly the mistake v1 §9 refused for the failure quorum.

### 2.8 Empty projects

Nine `project_contexts` carry no artifact at all (measured §2.1). Serving them costs a full
agent budget to produce zero consolidation.

**Proposed — and this is an inference, not an operator decision**: a project with zero mass
is **skipped**, with `last_attempt_date` still advanced and a `skipped_empty` reason logged.
The argument is D3 turned around: the ratchet D3 refuses assumes a consolidation
*would have produced something* that would then have been found, read, and surfaced. Zero
mass cannot bootstrap that cycle — skipping an empty project closes no loop. An artifact
created tomorrow makes the project non-empty and it gets served the next turn.

If the operator refuses this skip, the cost is quantified: nine projects × one agent budget
each, every ~3 rotation turns.

---

## 3. Human priority: order without filtering

### 3.1 Why a filter would be a ratchet — quantified

The operator decided (D3) and the argument is measurable. Human reads across **the entire**
corpus, all tables combined:

```sql
WITH a AS (SELECT access_count ac, access_count_human ach FROM learnings
           UNION ALL … decisions, snippets, runbooks, adrs)
SELECT count(*), count(*) FILTER (WHERE ac=0), count(*) FILTER (WHERE ac>0 AND ach=0),
       count(*) FILTER (WHERE ach>0) FROM a;
```

| | artifacts | share |
|---|---|---|
| total | **3,803** | 100% |
| **never read by anyone** | **1,180** | 31.0% |
| read by the machine only | **2,522** | 66.3% |
| read at least once by a "human" | **101** | **2.66%** |

A filter on the human counter would keep **2.66%** of the corpus. The 1,180 artifacts no
one has ever opened would never be consolidated, hence never surfaced in search,
hence never read. The loop would close on 97% of the brain. **D3 is confirmed by
measurement, not just by reasoning.**

At the project level it's even starker — 11 of 55 projects carry any human read at all:

| project | human reads | artifacts read | mass |
|---|---|---|---|
| red | 36 | 22 | 872 |
| red-shrik | 29 | 16 | 222 |
| red-monitor | 22 | 14 | 77 |
| brain-v42 | 21 | 19 | 659 |
| auto-discord | 11 | 9 | 113 |
| datalake-v1 | 9 | 8 | 74 |
| red-gift | 9 | 5 | 73 |
| red-arena | 4 | 4 | 25 |
| red-orchestrator | 2 | 1 | 10 |
| red-quant | 2 | 2 | 24 |
| red-lab | 1 | 1 | 161 |

Filtering means going from 55 projects to 11.

### 3.2 How it orders

Human priority is the **second** sort key (§2.4), inside the cursor's tie
band. It **never** decides eligibility.

Proposed definition of the key, per project:
`sum(access_count_human)` across the five tables, **and** `count(*) FILTER (WHERE ach>0)` as a
tiebreak. Two signals rather than one because they say different things: `red` has 36
reads over 22 artifacts (spread out), `red-shrik` has 29 over 16 (concentrated). The second is
more robust to a single, heavily-reread artifact.

**What it is not**: a weighting of mass. `red-lab` (161 artifacts, 1 read)
comes after `red-arena` (25 artifacts, 4 reads). This is intentional: the key measures
attention, not volume. Volume is the third key, and it only serves as a tiebreak.

### 3.3 What it does during the warm-up period

**The counter is two days old and has no backfill.** `access_count_human` arrived with 041,
applied on 2026-08-06 (CLAUDE.md, measured cutover); today is 2026-08-08. Full
distribution, measured:

```sql
WITH a AS (SELECT access_count_human ach FROM learnings UNION ALL … )
SELECT ach, count(*) FROM a GROUP BY 1 ORDER BY 1;
```

| `access_count_human` | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| artifacts | **3,702** | 72 | 18 | 7 | 3 | 1 |

**Three facts to accept during warm-up:**

1. **The key is near-constant.** 97.3% of artifacts are at zero, and 44 of 55 projects have a
   zero sum. The sort therefore degrades into `cursor → mass → key`. It doesn't become
   **wrong**, it becomes **blind**. And that's exactly why priority must be a sort key: a
   blind sort still covers the whole corpus; a blind **filter** would consolidate nothing
   for two days, then 2.66% of the corpus.
2. **The counter is monotone and never decays**: it can only go up. A project can therefore
   only **gain** rank as warm-up progresses; no project loses its place for a reason that
   isn't an actual read by someone else. Warm-up converges, it does not
   oscillate.
3. **No need to wait.** Nothing in the design depends on a warm counter; the
   cursor alone carries the coverage guarantee. Waiting for warm-up to ship would mean waiting
   on a signal that only shipping produces — human reads go through the same tools.

### 3.4 The counter's name lies, and it's measurable in the code

`src/brain_v42/provenance.py:26-27`:

```python
# Prefixes of system actors that self-declare. An actor absent from this
# list and not a sentinel is treated as human.
_SYSTEM_ACTOR_PREFIXES = ("dream-codex-",)
```

`access_count_human` therefore counts **any read that doesn't come from the nightly dream**,
including one from a third-party automated agent. §3.1's ranking must be read with this in
mind: `red-shrik` (29 "human reads", 2nd rank) carries a corpus of auto-generated supervision
alerts (measured sample: `vps cpu_total warning streak 1h`, `pc-serveur load_avg1 streak 1/h`, …).
A `red-shrik` agent rereading its own alerts is plausible, and it would count as human.

**Not measurable today**: `SELECT count(*) FROM access_log` returns **0**. This is normal —
`PgAccessLogRepo.aggregate_in_session` deletes aggregated rows and `purge_old(30)` finishes the
job — but the consequence is real: `access_log.actor`, the column 041 added
to make identity visible, **keeps no history**. There's no way to check after
the fact who read what. The counter is an aggregate with no log.

To do in the same batch, and it isn't cosmetic: **rename the notion in the reports**
("non-dream reads", not "human reads"), or extend `_SYSTEM_ACTOR_PREFIXES` to the
known agents. Until that's done, any table displaying "human" lies the same
way `dream_runs` used to lie about the project.

---

## 4. Decay: reuniting the two notions of freshness

### 4.1 The measured state, which isn't quite what the brief described

The brief stated: two notions that don't talk to each other, `freshness_status` written by an
LLM judgment, one code-side writer that resets to `fresh`. Reading the code and measuring the
corpus give a picture that is **more precise, and more troubling**.

**There are four writers of `freshness_status`, not one:**

| writer | file:line | what it writes |
|---|---|---|
| **the score itself** | `services/decay_flusher.py:204-215` | `new_status = self._decay_calculator.freshness_status(multiplier)`, written on UPDATE when it differs |
| **merging** | `services/consolidation.py:171` | `merged_into=target, freshness_status="archived"` on the source |
| REORG **judgment** | `scripts/dream/phase_reorg.md:82` via `brain_update` | `"archived"` only (`:117` forbids `fresh` and `stale`) |
| the **revive** | `mcp/tools/decay_tools.py:141` | `"fresh"` |

**So the link score → status already exists.** It's simply **starved twice**, and that's
the real design flaw.

**Starvation #1 — it only triggers on a read.** `DecayFlusher._flush` only recomputes
the entities surfaced by `aggregate_in_session`, i.e. those with a row in
`access_log`, i.e. those **that were just read**. The **1,180 artifacts nobody has
ever read** (§3.1) have never had their multiplier computed even once. D3's ratchet
therefore also exists at the entity level: *what nobody reads isn't even evaluated*.

**Starvation #2 — by the time it computes, the answer is already decided.** Arithmetic on
`decay.py`'s constants (not a runtime measurement): the flusher passes
`last_accessed_at = stats["max_accessed"]`, i.e. an instant a few minutes old.
`access_factor ≈ 1`. The multiplier's floor is therefore:

```
m ≥ w_access·1 + w_valid·0.7
learning / decision / snippet / runbook / plan : 0.30 + 0.14 = 0.44
adr                                            : 0.20 + 0.35 = 0.55
```

and `archive_threshold = 0.2`. **The flusher can mathematically never write `archived`.**
It can write `stale` (floor 0.44 < threshold 0.5), and that's what is observed:

```sql
SELECT project_key, created_at::date, updated_at::date, access_count, access_count_human,
       merged_into IS NOT NULL FROM snippets WHERE freshness_status='stale';
→ 3 rows, access_count=1, access_count_human=0, merged_into NULL
```

Three `stale` snippets, not merged, with exactly one machine read.
`phase_reorg.md:117` forbids REORG from writing `stale`: **these are flusher writes**, the
signature of the only score → status path that works. It works, and it can only produce one
of the three states.

### 4.2 The "troubling fact" is explained, and it reduces to a single line

The brief flagged 329 unexplained archivals (`red-shrik:agent` 205, `red` 124, …) even though
REORG only runs on `brain-v42`. Today's measurement — 359 archived learnings, and the
question is ill-posed:

```sql
SELECT merged_into IS NOT NULL, freshness_status, count(*) FROM learnings GROUP BY 1,2;
→ merged=t / archived : 348      merged=f / archived : 11
```

**348 of the 359 archivals are merge tombstones**, written by
`consolidation.py:171` — not a judgment, a mechanical side effect of the merge. The brief's
per-project count added up the tombstones and the judgments together.

The **11 real judgments**:

```sql
SELECT project_key, count(*) FROM learnings
WHERE freshness_status='archived' AND merged_into IS NULL GROUP BY 1;
→ brain-v42 : 10      red-shrik:agent : 1
```

Ten on `brain-v42` — the project REORG runs on. **A single one elsewhere**, and it's datable:

```sql
SELECT id, created_at::date, updated_at, access_count, left(topic,60) FROM learnings
WHERE freshness_status='archived' AND merged_into IS NULL AND project_key='red-shrik:agent';
→ a68af001… | 2026-04-21 | 2026-06-30 04:20:57+00 | 4 | test_phase2_red-shrik
```

and the corresponding night, in `dream_runs`:

```sql
SELECT phase, status, created_at, duration_s FROM dream_runs WHERE run_date='2026-06-30';
→ … promote 04:15:40 · reorg 04:21:47 (366 s)
```

The archival falls **at 04:20:57, inside that night's REORG window**
(≈04:15:41 → 04:21:47). The subject is `test_phase2_red-shrik` — test pollution, exactly
the family that `phase_reorg.md:64`'s whitelist authorizes archiving. And `brain_update` carries
**no project guard** while the scope is off (§1.3).

**Simplest explanation, compatible with every measurement: `brain-v42`'s nightly REORG
archived an entity from another project, once, on 2026-06-30.** No manual
`dream.sh <project>` run is needed to explain anything.

**What stays unproven, and is stated as such**: `updated_at` is rewritten by **every** UPDATE
on the row — `trg_learnings_updated` exists (`SELECT tgname FROM pg_trigger WHERE
tgrelid='learnings'::regclass`), and the flusher writes `access_count` without changing the
status. The window coincidence is a **bundle of clues**, not a trace. `dream_runs` has no
project column (042 isn't applied) and `access_log` is empty: **no audit trail exists
that could settle this**, and there won't be one for the past.

### 4.3 What enters the night: the link, not the computation

**Decided, and this sentence is §4's main constraint: the computation stays on-the-fly.**
`brain_service.py:336-347` computes the multiplier on every search, with a
`last_accessed_at` fresh to the millisecond. Persisting it overnight would make it stale by
24 h. That would be a **regression**, not an optimization.

What enters the loop is **the link**: the score produces candidates, REORG judges.

**Interface, precisely.** A **read-only** candidate producer, called by REORG
at the start of its Part 2:

```
brain_decay_candidates(project_key, limit=20, entity_types=[...])
→ [ { id, type, topic, multiplier,
      age_factor, access_factor, freq_factor, validation_factor,   ← the four terms, separate
      access_count, access_count_human, age_days, last_access_days,
      freshness_status } … ]   sorted by multiplier ASC
```

Seven mandatory properties:

1. **One computation in the repo.** The tool calls `DecayCalculator.compute_multiplier`, it
   does not re-implement the formula in SQL. This spec's SQL replica (§0) exists to measure,
   not to be copied: two formulas drifting apart is the defect being
   fixed here, not a method.
2. **The four terms are rendered separately.** A judge handed `0.2231` can do nothing
   with it. `age_factor=0.03 · access_factor=0.03 · freq=0.00 · valid=0.70` reads: *old,
   never reread, never validated*.
3. **Rank, not threshold.** See §5.4: `archive_threshold=0.2` selects **nothing** on this
   corpus (measured minimum: 0.2228). The producer returns the N lowest, where N is bounded
   by REORG's existing cap (20, `phase_reorg.md:85`).
4. **The judge stays, and the verdict stays reversible.** The score **proposes**, REORG
   **decides**, and the decision is `freshness_status='archived'` — reversible via revive
   (`decay_tools.py:141`). Nothing irreversible enters this path; the irreversible is in §6.
5. **REORG's counter guard must change, but not for the reason first published.**
   `phase_reorg.md:119`: "**NEVER archive** an entity with `access_count > 5`" — the
   **total** counter; the same rule is repeated at step (b) of Part 2 (`access:N` where `N > 5`).
   Measured: 508 of 2,742 learnings (18.5%) have `access_count ≥ 10`, and **zero** have
   `access_count_human ≥ 10`. The first draft concluded: "shipping the producer without this
   change means wiring a nominator into a veto that cancels it out". **The review measured
   the intersection, and it's empty**:

   | rank in the producer's ranking | candidates with `access_count > 5` |
   |---|---|
   | first 20 (the specified `limit`) | **0** |
   | first 100 | **0** |
   | first 500 | 23 (human counter) · 0 (total counter) |

   The veto only bites from **rank 264** on with the human counter, and never before rank
   1,000 with the total counter. The reason is structural: a high `access_count` comes with a
   recent `last_accessed_at`, hence an `access_factor` close to 1, hence a **high**
   multiplier — the vetoed set and the nominated set sit at opposite ends of the same ranking.
   The guard still needs changing (it protects against auto-generated pollution, and that's
   the point of 041), but **it doesn't block the producer** and it isn't a shipping precondition.

6. **The real gate is the whitelist — and it closes 100% of the time.** This is the design
   flaw the first draft missed. Part 2 of `phase_reorg.md:64-71` only authorizes
   REORG to archive entities whose subject matches one of six patterns:
   `^test_`, `^verify_.*_test$`, `^infra_status`, `^status_infra_`, `^cpu_metrics_`,
   `^.*_events_\d+h$` — and step (a) requires "checking the regex match" before anything
   else. Measured, by applying the six patterns to the producer's ranking:

   | population | matches the whitelist |
   |---|---|
   | 20 lowest multipliers | **0** |
   | 100 lowest | **0** |
   | 500 lowest | **0** |
   | entire non-archived corpus (2,343 learnings) | **7** |

   **Zero out of five hundred.** §5.4's bottom of the ranking (`Neo4j Persistence Fix`,
   `Grafana metrics fix`, the nine `Lyriks Design Conventions …`) matches no pattern. Shipped
   as-is, `brain_decay_candidates` hands REORG twenty entities every night that its own prompt
   forbids it to archive: **the link is a no-op, not a link.**

   **Decided: the batch does not ship without deciding Part 2's mandate.** Three options, and
   one must be named:

   - **(a) Change nothing about the whitelist.** The producer becomes a *reporting*
     instrument: REORG reads it, logs it in the nightly report, does not act. Honest, no risk,
     and already better than nothing — but it's no longer "the score proposes, REORG decides".
   - **(b) Widen the whitelist.** This changes the mandate: Part 2 goes from "archive
     known pollution by pattern" to "archive what the score names and the judge
     finds stale". The last safeguard then becomes an LLM's judgment on text, and the
     verdict feeds, 180 days later, into §6.6's **irreversible** path. **This is
     exactly the automatic purge that dares not speak its name**, which §6.1 refuses. If this
     is the option chosen, it ships with its own killswitch and its own soak, never at the same
     time as the producer.
   - **(c) A third verdict.** The producer doesn't name candidates for archival but
     candidates for **human review**: a report output, not a mutation. This doesn't require
     no whitelist change and leaves §6.6 intact.

   - **(d) Refuse the coupling, and fix REORG's scope separately.** Option absent from the
     first draft, opened up by §12.1's measurement: Part 2 has never hit its own
     targets, independently of any producer. The flaw isn't the list's width, it's the
     500-row window sorted by `created_at`. Fix the scan, the list doesn't
     move a single character, and the producer stops being the subject.

   **Review recommendation: (a) for step 6 of §8, and (c) as the report's shape.**
   Option (b) is a separate operator decision, and it hasn't been made.

   **Operator decision of 2026-08-08 (§12.3): (d), with (c) as the report's shape.** This
   supersedes the recommendation above without contradicting it: (a) proposed making the
   producer harmless by disconnecting it from the action, (d) notes that connecting it could
   never have produced anything anyway — the two populations don't overlap (§12.2). The
   practical difference is that (a) left REORG broken and (d) fixes it. **(b) remains undecided**,
   and becomes even less likely: widening wouldn't reconcile two sets that are disjoint by
   construction.

7. **The producer is itself a ratchet, and nothing makes it rotate.** The twenty lowest all
   have `access_count=0` and `access_count_human=0` (measured): their multiplier only
   ever decreases, monotonically, and their relative order never changes. A rejected candidate
   therefore comes back **the next day, in the same place, indefinitely**, and forever hides
   rank 21. The twentieth multiplier is 0.2279 and the hundredth is 0.2664 — the gap is
   so thin that no reasonable aging reorders the front of the line. The repo already has the
   answer to this exact problem: `roadmap_curate.rotate_keys` (`:474-487`), written on
   2026-07-04 because "`ORDER BY` + `LIMIT` scanned the first 10 alphabetical projects every
   night and never the other 16". **The producer must carry the same deterministic offset**,
   or memorize rejections. Without either, `limit=20` is a frozen window over 2,343 entities.

**The missing return path, in the other direction.** Today the persisted status never weighs
on the score: an `archived` entity is simply excluded from search
(`brain_service.py:280,290-299`, `include_archived=False` by default). That's enough for
search. It is **not** enough for §6, and here's why it's a blocker:

**there is no archival date on the entity's row.** `freshness_status` is a column with no
timestamp of its own. `updated_at` moves on every counter write by the flusher —
`trg_learnings_updated` is a `BEFORE UPDATE` **with no `WHEN` clause** (`pg_get_triggerdef`, measured).
And the only existing deletion criterion relies on it (`decay_tools.py:83-95`,
`updated_at < now() - 180d`). **Purge therefore runs on a clock that any machine read
resets to zero.**

**Review correction: "there is NO honest clock" was too strong.** One exists,
imperfect, and it's already in production:

```
CREATE TRIGGER learnings_brain_entity_registry_trigger
  AFTER INSERT OR DELETE OR UPDATE OF topic, project_key, freshness_status, merged_into, metadata
  ON public.learnings FOR EACH ROW EXECUTE FUNCTION sync_brain_entity_registry()
```

The function writes `brain_entities.updated_at = NOW()` (`prosrc`, lines 82 and 230). **`access_count`
and `last_accessed_at` do not appear in the column list**: the flusher does not trigger this
trigger. `brain_entities.updated_at` is therefore a clock **immune to exactly the
contamination** this paragraph denounces. Verified on the 98 learnings pending §6.2: the 98
are matched in the registry, `min(brain_entities.updated_at)` is **2026-03-13**, the same date
as `min(learnings.updated_at)`, and **0** exceed 180 days. The registry also carries
`lifecycle ∈ {active, archived, deleted}` — 2,343 / 399 / 3 for the learnings, and
399 = 359 archived + 40 merged-but-fresh.

It remains imperfect, and that's why the dedicated column stays decided: Part 1 of REORG
**normalizes `tags` and `project_key`**, and `project_key` is in the trigger's list — a
metadata tidy-up would therefore rejuvenate the archival clock. But the gap between "no
clock" and "a clock with four contaminating writers instead of all" changes the timeline:
§6.6 can start measuring a stay **today**, approximately, instead of waiting
several months after the column ships.

**Decided: a `freshness_status_updated_at` column (and, while at it, `freshness_source ∈
{merge, judgment, score, revive}`) on the six tables the decay tracks.** When `updated_at`
can't answer, date the thing itself, no backfill, `NULL` = "never measured".

**The mechanism is 041's, not 040's, and this must be said.** The first
draft spoke of "the 040/041 doctrine" without choosing. The two migrations made
opposite choices for a reason CLAUDE.md names: 040 writes `focus_updated_at` **in
application code** because the focus has **only one** writer; 041 writes `content_updated_at`
via a **conditional trigger `WHEN … IS DISTINCT FROM`** because content has many.
`freshness_status` has **four** (§4.1), one of which — the REORG judgment — goes through the
generic `brain_update` tool, which knows nothing about decay. Stamping in application code
would require doing it inside `brain_update` itself, for a column 99% of its calls don't touch.
**So 041 is what gets copied**: a `BEFORE UPDATE OF freshness_status … WHEN (old.freshness_status
IS DISTINCT FROM new.freshness_status)`, on the same template as `trg_learnings_content_updated`,
which already exists and can be read. None of the four writers then has to remember it — and
that's the point, since one of the four is a prompt.

This is where the two notions meet, and it's **the hard precondition for purge**.

---

## 5. Recalibrating the formula, and where the values come from

### 5.1 The contaminated signal — re-measured

```sql
SELECT count(*), sum(access_count), sum(access_count_human), max(access_count_human),
       count(*) FILTER (WHERE access_count>=10), count(*) FILTER (WHERE access_count_human>=10)
FROM learnings;
```

| table | n | Σ total | Σ human | human share | max hum | saturated (total) | saturated (human) |
|---|---|---|---|---|---|---|---|
| learnings | 2,742 | 19,049 | 79 | **0.41%** | **5** | 508 (18.5%) | **0** |
| decisions | 843 | 7,994 | 41 | 0.51% | 3 | 182 | 0 |
| snippets | 100 | 388 | 6 | 1.55% | 3 | 3 | 0 |
| runbooks | 93 | 965 | 13 | 1.35% | 2 | 50 | 0 |
| adrs | 25 | 224 | 7 | 3.13% | 3 | 19 | **1** |
| indexed_plans | 197 | 508 | 11 | 2.17% | 3 | 25 | 0 |

("Saturated" column = above the `freq_baseline` **for that type's profile**: 10 for
learning/decision, 20 snippet, 5 runbook/plan, 3 adr.)

The brief gave max human = 4 for learnings; today's re-measurement gives **5**. The
counter moves while it's being written down — one more reason not to copy a number forward.

`brain_service.py:336` passes `access_count = getattr(entity, "access_count", 0)` — the **total**.

### 5.2 The hole the brief doesn't name: the recency term weighs more and has no human variant

Swapping the counter only fixes `w_freq`. But `access_count` and `last_accessed_at` are
**two** inputs, weighted **0.2** and **0.3**:

| term | weight (learning) | source | fixable by 041? |
|---|---|---|---|
| `age_factor` | 0.3 | `created_at` | neutral |
| `access_factor` | **0.3** | **`last_accessed_at`** | **no — the human column doesn't exist** |
| `freq_factor` | 0.2 | `access_count` | yes — `access_count_human` |
| `validation_factor` | 0.2 | `validated_at` / `decided_at` | neutral |

`\d learnings` confirms it: there is `access_count_human`, there is **no**
`last_accessed_at_human`. And the contamination is massive:

```sql
SELECT count(*) FILTER (WHERE last_accessed_at IS NOT NULL AND access_count_human=0),
       count(*) FILTER (WHERE last_accessed_at IS NOT NULL AND access_count_human>0),
       count(*) FILTER (WHERE last_accessed_at IS NULL) FROM learnings;
→ 1,779   |   51   |   912
```

**1,779 learnings have their recency term — the heaviest along with age — driven by
machine reads alone.** Fixing `freq` alone repairs 0.2 of the 0.5 of weight driven by
reads. The rest keeps being kept alive by what the machine rereads.

**Good news, measured: the aggregate already knows everything it needs.**
`repositories/pg_access_log.py:63-94` already groups by `access_log.actor`, tests
`is_human_actor(row["actor"])` to fill `count_human`, and computes `max_accessed` — but it
**folds** that max over every actor. A `max_accessed_human` is one line in an existing
loop. **What's missing is the column to put it in**, on the same six tables as 041.

### 5.3 The proposed values, and their origin

**Principle, to be written into the code**: a `freq_baseline` is derived from **the
distribution of the counter being wired in**, never from the ratio between the two counters.
The ratio (0.41%) would give `10 / 240` — nonsense.

Measured distribution of the human counter across the whole corpus (§3.3): `0→3,702, 1→72,
2→18, 3→7, 4→3, 5→1`. The global maximum is **5**, and **11 of 3,803 artifacts** reach 3.

| profile | current `freq_baseline` | proposed | value's origin |
|---|---|---|---|
| learning | 10 | **3** | max human measured 5; `≥3` = 11 corpus-wide artifacts (0.29%). A baseline of 3 makes the term work across its full range without saturating on the first access |
| decision | 10 | **3** | max human measured 3 |
| snippet | 20 | **2** | max human 3, six human reads across 100 snippets |
| runbook | 5 | **2** | max human 2 — at 3 the term could never be full |
| plan | 5 | **2** | max human 3 |
| adr | 3 | **3 (unchanged)** | the only type whose human counter already saturates (1 ADR): the value is already calibrated for this counter |

These aren't physical constants. They should be re-measured — and the repo has the place to
say so: `src/brain_v42/thresholds.py` already carries `calibrated=False` / `last_calibrated=None` /
`corpus_dependency` on thresholds of the same kind (`consolidation_similarity`, line 105-118).
These six baselines go there, with `corpus_dependency="access_count_human distribution"` and the
measurement date.

**What the review measured, and which reframes this whole subsection: on today's
corpus, changing the baselines does almost nothing.** The real `DecayCalculator` run on
the 2,343 learnings, human counter, `freq_baseline` 10 then 3:

| | baseline 10 | baseline **3** |
|---|---|---|
| multiplier **changed** | — | **51 of 2,343 learnings (2.18%)** |
| multiplier unchanged | — | **2,292** |
| mean | 0.4493 | 0.4507 |
| minimum | 0.2228 | **0.2228** (identical) |
| p10 | 0.2905 | **0.2905** (identical) |
| below 0.5 → `stale` | 1,453 | **1,453** (identical) |
| below 0.25 · below 0.30 | 49 · 263 | **49 · 263** (identical) |

The reason is arithmetic and fits in one line: `frequency_factor = min(ac / baseline, 1)`, and
**3,702 of 3,804 artifacts have `access_count_human = 0`**. Zero divided by any
baseline is zero. The baseline only touches the **51 learnings** that have at least one
human read — the same 51 as §5.2 — and pushes them **up** (max delta +0.1400).

Two consequences to spell out:

- **The entire measured effect of §5.4 (0.5130 → 0.4493, +359 `stale`) comes from SWAPPING the
  counter, not from the baselines.** These are two changes of a different nature packaged in
  the same subsection, and only one of them has a measurable effect today.
- **The baselines change nothing about the candidate ranking** (§4.3): the bottom of the
  ranking is entirely at `access_count_human = 0`, hence `freq_factor = 0`, hence invariant.
  They are a **protective** change — they lift the 51 artifacts a human has opened — and
  must be sold that way, not as a recalibration of the archival signal.

They stay justified: once the counter warms up, a baseline of 10 against an observed
maximum of 5 would make the term structurally unable to fill up. But **they aren't urgent,
and they unblock nothing**. If step 6 of §8 needs to be split to reduce its risk, this is
where the cut line runs.

### 5.4 What recalibration does NOT do: create candidates for archival

This spec's central measurement. **At-rest** multiplier (with the real `last_accessed_at`, not
one from an access in progress), over the 2,343 learnings neither archived nor merged. First
published from a SQL replica, **since recomputed from the real `DecayCalculator` imported
from the repo** (§0): the two agree, only the human p10 differs by 0.0001. The values below
are from the real code.

| | **total** counter | **human** counter |
|---|---|---|
| mean | **0.5130** | **0.4493** |
| minimum | **0.2228** | 0.2228 |
| p10 | 0.2998 | 0.2905 |
| below 0.5 → `stale` | **1,094** | **1,453** |
| below **0.2** → `archived` | **0** | **0** |

**The corpus minimum (0.2228) is above the archival threshold (0.2).** Swapping the
counter pushes 359 more learnings into `stale` and **still archives none of them**.

Arithmetic of the reason, on `decay.py`'s constants: an unvalidated entity gets
`w_valid · 0.7 = 0.14` **for free**, and the oldest corpus entry dates from 2026-01-05 (215
days), i.e. `age_factor = 2^(-215/90) ≈ 0.19` → `0.3 · 0.19 ≈ 0.057`. The corpus's real floor
is therefore around 0.22. The 0.2 threshold was chosen for a corpus that doesn't exist yet.

**Decided: the candidate producer ranks, it doesn't threshold** (§4.3, property 3). Two
reasons: a threshold's population isn't predictable (at 0.25 → 49 learnings, at 0.30 → 236 —
measured), whereas a rank's population is exactly REORG's cap. And a rank doesn't
go stale as the corpus ages.

`stale_threshold` and `archive_threshold` **stay** — for `brain_service.py:347`'s `_freshness`
label and for `format_decay_status`. They stop being a gate.

And the produced order is already good, even before the counter warms up. Bottom of the
ranking, measured (real `DecayCalculator`, human counter):

```
0.2228  datalake-v2     Neo4j Persistence Fix                       ac=0 ach=0  2026-01-05
0.2228  datalake-v2     Service Dependency Injection Pattern        ac=0 ach=0  2026-01-05
0.2228  second-cerveau  Second Cerveau cleanup                      ac=0 ach=0  2026-01-05
0.2228  second-cerveau  Grafana metrics fix                         ac=0 ach=0  2026-01-05
0.2233  poc-lyriks-v2   neo4j-driver v5 breaking changes            ac=0 ach=0  2026-01-06
0.2241  lyriks          Lyriks Design Conventions - Grid System      ac=0 ach=0  2026-01-07
…
20th = 0.2279 · 100th = 0.2664 · 500th = 0.3132
```

The four projects at the bottom of the ranking are the ones whose last artifact dates from
149 to 215 days ago (§6.2). **The score already knows what to propose; it has no one to tell
it to.** That was the sentence that summed up §4 — and the review corrected it on two
points, both in §4.3: it **does** have someone to tell (the score → status link already
exists via the flusher), and the judge it's meant to tell **isn't allowed** to archive what
it's offered (property 6: zero of the 500 lowest match REORG's whitelist). The ranking is
good; the recipient is still to be chosen.

### 5.5 The only change in this spec that touches an interactive path

Changing the `DecayProfile`s changes `effective_score` (`brain_service.py:344`) for **all**
searches, immediately, including one from a human in a session. There is **no** dry-run
for search, and `decay_floor=0.3` (`config.py:192`) dampens without cancelling.

**Decided**: the counter swap and the new baselines ship **behind a setting**, with
today's values as the default, and a before/after measurement over a fixed set of
queries. This isn't an irreversibility killswitch (nothing is written), it's a
**relevance-regression** killswitch — the only thing in this batch a human would notice the
same day.

---

## 6. Purge — a dedicated section, because it's irreversible

### 6.1 Posture, before any figure

Same posture as `sweep` (v1 §8), non-negotiable:

- shipped **closed killswitch**: `BRAIN_DREAM_PURGE_ENABLED=false`, `BRAIN_DREAM_PURGE_DRY_RUN=true`,
  **absent from the drop-in**;
- **dry-run first**, with a full manifest of what would be deleted;
- **measuring what it would touch BEFORE arming**, published, not deduced;
- **never armed in the same batch** as a topology change, nor as `sweep`'s opening.

### 6.2 What it would touch today: zero

The repo **already** has a deletion criterion, and no one has noticed.
`mcp/tools/decay_tools.py:83-95`, in `brain_decay_status()` — hence **already shown to the
SCAN agent every night**:

```python
# Deletion candidates: archived 180+ days with access_count=0
table.c.freshness_status == "archived", table.c.access_count == 0, table.c.updated_at < cutoff
```

Replayed as-is:

```sql
SELECT count(*) FILTER (WHERE freshness_status='archived' AND access_count=0
                          AND updated_at < now()-interval '180 days') FROM learnings; -- and the 4 others
```

| table | candidates **today** | archived at `access_count=0` pending |
|---|---|---|
| learnings | **0** | 98 |
| decisions | **0** | 2 |
| snippets | **0** | 0 |
| runbooks | **0** | 1 |
| adrs | **0** | 0 |

**Zero, everywhere.** And the date this changes is computable:

```sql
SELECT min(updated_at)::date, (min(updated_at)+interval '180 days')::date, count(*)
FROM learnings WHERE freshness_status='archived' AND access_count=0;
→ 2026-03-13 | 2026-09-09 | 98
```

**First candidate: 2026-09-09**, in 32 days, then 98 learnings that follow. A purge
armed today would do nothing; armed unwatched, it would bite on September 9th.

**What these 98 learnings really are — the measurement the first draft didn't make, and
which flips the paragraph:**

```sql
SELECT count(*), count(*) FILTER (WHERE merged_into IS NOT NULL)
FROM learnings WHERE freshness_status='archived' AND access_count=0;
→ 98 | 97
```

**97 of the 98 are merge tombstones.** The existing criterion, left as-is, would
therefore not delete "98 stale pieces of knowledge": it would delete **the history of 97
merges done by CLEAN**, plus a single real entity —

```sql
SELECT project_key, created_at::date, left(topic,40) FROM learnings
WHERE freshness_status='archived' AND access_count=0 AND merged_into IS NULL;
→ brain-v42 | 2026-07-26 | Dream night failure: 2026-07-26
```

This is exactly the danger §6.5 names ("any rule must exclude `merged_into IS NOT NULL`")
applied to this spec's only dated blast radius. **With tombstones excluded, 2026-09-09 no
longer concerns 98 entities but 1.** Two orders of magnitude. The paragraph
"armed unwatched, it would bite on September 9th" stays true, but what it would bite
isn't what we thought: the trace of consolidation work, not its stale output.

**This existing criterion is wrong on both its terms**, and §4.3/§5.2 explain why:

- `access_count = 0` — the **total** counter. An artifact reread only by the dream falls
  out of the criterion and becomes indefinitely non-purgeable. This is §5.1's mechanism applied
  to deletion.
- `updated_at < cutoff` — the 180-day clock **restarts on every flusher counter write**
  (`trg_learnings_updated` is present). Without `freshness_status_updated_at` (§4.3), **no**
  honest clock exists. **Purge is blocked on this column**, this isn't a scheduling preference.

### 6.3 The risk the operator named, re-measured

> "Across 46 projects the dream has never read, a first WET pass would erase what was
> never looked at."

Re-measured, it's **worse than 46**: the dream has never synthesized anywhere but `brain-v42`
(§2.1, `dream:generated` → 87 learnings, a single project). That's **54 of 55 projects**, and
**3,145 of 3,803 artifacts (82.7%)** that have never seen a consolidation phase.

And for the most likely definition of "inactive" — no artifact created in 90 days:

```sql
WITH a AS (… the five tables …), last AS (SELECT project_key, max(created_at) lc FROM a GROUP BY 1)
SELECT count(DISTINCT a.project_key), count(*), count(*) FILTER (WHERE a.access_count_human>0),
       count(*) FILTER (WHERE a.access_count>0)
FROM a JOIN last l USING (project_key) WHERE l.lc < now() - interval '90 days';
```

| | value |
|---|---|
| **artifact keys** with no new artifact in 90 d | **31** (the domain here is keys carrying artifacts, 54 non-null values, not the 55 `project_contexts`) |
| artifacts involved | **567** |
| of which read at least once by a human | **3** |
| of which read at least once by anyone | 268 |

> **This blast radius shifts visibly.** Replayed twenty minutes later, the same SQL returns
> **32 keys / 580 artifacts / 3 human / 271 anyone**: the key `perso` (13 artifacts created from
> 2026-02-17 to 2026-03-20) entered the set between the two measurements. A figure that varies
> by 2.3% in twenty minutes is not a basis for arming an irreversible deletion — it's one more
> reason to have this manifest produced **by the purge itself, in dry-run, at the moment
> it would actually run**, never by a document.

**A WET pass on "project inactive 90 d" would delete 567 artifacts, 3 of which have been
looked at by a human.** This isn't an argument for purging: it's the opposite argument. These
567 artifacts were never consolidated, hence never indexed by CONNECT, hence never surfaced by
search — **they never had a chance of being read.** Deleting them means concluding from a
silence we produced ourselves.

Counter-example, measured, worth keeping in view: `red-orchestrator`, last artifact
**120 days** ago, still carries **2 human reads** in two days of counter (§3.1).
Write inactivity says nothing about read inactivity.

### 6.4 Decided: the cursor is purge's entry ticket

**A project is only purgeable if the dream has consolidated it.** Formally:
`dream_project_cursor.last_success_date IS NOT NULL`, and for enough nights that
consolidation could have produced something.

This is what links D4 and D5: "we only purge what we've looked at, and the cursor is the
proof we looked at it". Today, this rule makes **54 of 55 projects non-purgeable**, and
that's the right answer. It relaxes on its own as the cursor turns — nothing needs to be
re-armed.

### 6.5 Two purges, two blast radii — never to be confused

**Entity purge.** The primitive already exists: `mcp/tools/crud_tools.py:280`,
`brain_delete(entity_type, entity_id)`. No new destructive tool is required. Three
measured couplings:

```sql
SELECT tc.table_name, kcu.column_name, ccu.table_name, rc.delete_rule …
WHERE ccu.table_name IN ('learnings','decisions','snippets','runbooks','adrs','project_contexts');
```

- `dream_promotions` carries **four** `SET NULL` FKs to the knowledge tables, not one:
  `source_learning_id → learnings`, `target_runbook_id → runbooks`, `target_adr_id → adrs`, plus
  `dream_run_id → dream_runs` (105 rows in `dream_promotions`, measured). Deleting a learning
  **silently empties the promotion audit trail** — and deleting a **target** runbook or ADR
  empties it too, from the other end. The exact same risk v1 §12 refused for `dream_run_id`.
- `merged_into` is `SET NULL` on the five tables: deleting a merge target **unties its
  tombstones**, which turn back into archived entities with no explanation.
- **Tombstones are the first target of a naive criterion.** 348 of the 359 archived learnings
  are merge tombstones (§4.2). A purge on `freshness_status='archived'` destroys
  **the merge history** first, i.e. the trace of everything CLEAN has consolidated.
  Any rule must exclude `merged_into IS NOT NULL`, or say why it doesn't.
- **The graph**: `services/graph_helpers.py:210 graph_delete_entity` exists; a purge that
  forgets it leaves orphan Neo4j nodes. Not measured here (§10).

**Project purge.** A different radius, and **two** structural guards already in place — the
first draft only named one. Full inventory of incoming FKs, measured:

```sql
SELECT tc.table_name, kcu.column_name, ccu.table_name, rc.delete_rule … 
WHERE ccu.table_name IN ('projects','project_contexts');
→ brain_sessions.project_key  → project_contexts : RESTRICT
→ brain_entities.project_key  → projects         : RESTRICT
→ project_aliases.project_key → projects         : CASCADE
```

- `brain_sessions → project_contexts` is **`RESTRICT`**, with **363 sessions across 17 projects**.
  **Seventeen of 55 `project_contexts` cannot be deleted at all**, and PostgreSQL will
  refuse fail-closed with nothing needing to be written.
- **`brain_entities → projects` is `RESTRICT`, which has nothing to do with it and blocks
  everything.** `projects` and `project_contexts` are two distinct tables. Measured: **74 rows
  in `projects`**, and **74 distinct keys referenced by `brain_entities`** — every project
  carries at least its own registry node (`entity_type='project'`, 74 rows), which references
  its own key. **All 74 rows of `projects` are therefore already indelible as-is**, and 59
  of them additionally carry knowledge entities. A "project purge" must say which of the two
  tables it targets; if it's `projects`, it's structurally impossible without dismantling the
  registry — which is itself protected by `entity_relations`'s `RESTRICT`.

The 9 empty contexts (§2.1) are the only ones whose deletion destroys nothing but
themselves — if they have no session, and provided only `project_contexts` is targeted.

### 6.6 The staircase, and where it's cut off today

```
propose → archive (reversible) → minimum stay in archive → purge (irreversible)
   ↑ §4.3        ↑ REORG, cap 20         ↑ BLOCKED               ↑ closed, dry
```

The third step **does not exist on the entity's row**: there's no dedicated archival date
(§4.3), hence no reliably measurable stay, hence no defensible purge. **The "purge" batch
cannot start before `freshness_status_updated_at` has shipped and has accumulated real
time.** There is an incompressible wait of several months between the column and
the first WET here — to be told to the operator now, not at the moment of arming.

**Nuance added by the review**: `brain_entities.updated_at` (§4.3) already gives, today, an
approximation of the stay, immune to the flusher but not to REORG Part 1's metadata
normalization. This doesn't shorten the soak — an approximate figure doesn't justify a
deletion — but it does let us **watch the step while it's being built**, instead
of being blind until the dedicated column's first measurement.

And the dry-run manifest must not end up in `logs/dream/`: v1 §16 measured
**1,865 files for 121 MB with no rotation at all** there. A deletion manifest is an audit
artifact; it goes into the database or a dedicated path.

---

## 7. The 05:00-09:00 window: timer, ceiling, and the regenerated-template trap

### 7.1 The measured state

| what's in force | value | where it lives |
|---|---|---|
| trigger | `OnCalendar=*-*-* 06:00:00` | `~/.config/systemd/user/brain-v42-dream.timer:7` **and** `deploy/systemd/brain-v42-dream.timer:7` |
| jitter | `RandomizedDelaySec=120` | same `:13` |
| catch-up | `Persistent=true` | same `:10` |
| hard ceiling | `TimeoutStartSec=10800` (**3 h → kills at 09:00**) | `~/.config/systemd/user/brain-v42-dream.service:38` **and** `deploy/systemd/brain-v42-dream.service.tmpl:41` |
| execution target | `dream.sh brain-v42` | `…service:28`, `…tmpl:31` |

`systemctl --user show` wasn't available in this environment (no DBus bus); the
values above are read **from the live files** under `~/.config/systemd/user/`, which is the
same source but without drop-in overrides. No drop-in touches `TimeoutStartSec`
(`grep -rn TimeoutStartSec` over the three `.conf` → nothing).

**Count correction.** The first draft published "`killswitches.conf` 8" from
`grep -c 'Environment='`. That pattern isn't anchored and counts line 3 of the file, which is a
**comment** (`# Environment= line there (incident 2026-06-30 …)`). The real count of active
directives is **7** (`grep -c '^Environment='`), and that's also what §7.2's guard would
return, whose awk skips lines starting with `#` or `;`. `nvidia.conf` 0, `token.conf` 0.

**Moving to 05:00-09:00 requires moving both**, and they are two different files.

### 7.2 The trap, verified by reading the guard

`deploy/systemd/install.sh` regenerates the unit from the template. Its guard is
`warn_wiped_env` / `count_environment_directives` (`install.sh:277-345`), read in full: it
counts **`Environment=`** lines and nothing else.

**Consequence: a `TimeoutStartSec` bumped by hand in
`~/.config/systemd/user/brain-v42-dream.service` gets rewritten to 10800 by the next
reinstall, without a word.** The following night would be killed at 08:00 (3 h after
05:00), in the middle of the window, leaving unserved projects with **no row at all in
`dream_runs`** — a `DISTINCT ON (phase)` reader would see nothing abnormal. This is the exact
twin of the 2026-06-30 incident cited at the top of `killswitches.conf` (PROMOTE+REORG off
for two nights from a regeneration).

The timer suffers the same mechanics: `brain-v42-dream.timer` is in the list of units
copied by `install.sh` (`:44`).

**Decided: both values move in `deploy/systemd/`, never in the live copy.** And the
integration test `tests/integration/test_dream_systemd_install.sh` (v1 §13: `:180` pins
`ExecStart=… dream.sh brain-v42`, `:284,286` the preflight) is updated **in the same commit**.

### 7.3 The window's budget, and who enforces it

240 minutes. Proposed breakdown, figures from §2.1:

| item | budget | origin |
|---|---|---|
| global phases (extract 10 + roadmap 20 + sweep 5) | **35 min reserved** | **configured** ceiling of the three `timeout`s (v1 §4.3, `dream.sh:664,706,735`) — a hard bound, not an average |
| project loop | **205 min** | the rest |
| per-project admission budget | **15 min** | measured p90 of one night's agent subtotal: **14.73 min**, rounded up |

**Projects served per night**: `205 / 14.73 = 13.9` at p90; `205 / 9.28 = 22.1` on average;
`205 / 30.50 = 6.7` at the worst measured agent subtotal. Coverage of 55 projects: **2.5 nights
on average, 4.0 nights at p90, 8.2 nights at the worst measured case**.

> **These three figures ignore the never-served cap from §2.5**, and the cap bites
> exactly at cutover, when all 55 projects are new. Simulated over the 55 real projects:
> at window 14 with a fixed half-cap (7), full coverage falls to
> **night 8**, not 4, and the first night only admits 7 projects for a window of 14. §2.5 was
> corrected accordingly: the cap only applies if there are already-served projects to
> fill the rest. With this correction, and only with it, do the figures above hold.

The operator had computed ~25 project-units (D4) starting from the 9.29 average and without
reserving the globals. **The arithmetic is right; it describes the average.** The spec plans
on the p90, i.e. ~14, because a coverage plan that only holds one night out of two isn't a
plan. Both figures are valid, they just don't answer the same question.

**Decided: the window is enforced by the loop, not by systemd.** The loop carries a
deadline and only admits a project if `remaining ≥ admission budget`. `TimeoutStartSec=14400`
(09:00 sharp from 05:00) becomes the **safety net**, not the mechanism. Measured reason: v1
§4.3 — systemd kills **in the middle of a project**, and the following projects write no
row at all; the failure is then invisible. A loop that itself decides not to admit a project
**leaves it at the head of the cursor**, which is a correct and readable state.

**The deadline is relative to process start, NOT a wall-clock time.** The first draft
proposed "an absolute deadline (e.g. 08:55)". The review connected this sentence to the
"catch-up" line of §7.1's table, two paragraphs above:

```
~/.config/systemd/user/brain-v42-dream.timer:10   Persistent=true
```

`Persistent=true` replays a missed occurrence **on the next startup, at any time at
all**. A PC turned back on at 13:00 triggers the 05:00 dream at 13:00. A wall-clock deadline at 08:55 then makes `remaining` **negative**: the loop admits **no** project,
advances **no** cursor, and exits green with no row at all in `dream_runs` — the exact
twin of the silent failure mode this very paragraph says it wants to avoid. Repeated, this
is zero coverage that never reports itself.

**Decided**: `deadline = process_start + 235 min`, bounded by `min(deadline, 08:55)` **and**
by a floor guaranteeing at least one project. A late catch-up then serves a full window
outside the nominal slot — which is `Persistent=true`'s intended behavior — instead of
serving nothing. If the operator prefers a late catch-up not to run at all, the way to
write that is `Persistent=false`, not a wall-clock deadline that fails silently.

**Retry falls in the same budget.** v1 §10: retry costs +43 min per project
(`dream.sh:539`, hard failure only, never on timeout, never on `promote`). At 14 projects
that's +602 min at the ceiling, two and a half times the window. v1's recommendation
holds and hardens: **a retry allocation for the whole night**, deducted from remaining
time like any phase.

### 7.4 What the new window doesn't collide with

Neighboring timers, read in `deploy/systemd/`:

| unit | trigger | jitter | ceiling | worst-case end |
|---|---|---|---|---|
| `brain-v42-embedding-backfill` | `*-*-* 04:30:00` | 120 s | `TimeoutStartSec=900` | ~04:47 |
| `brain-v42-graph-recon` | `Sun *-*-* 04:00:00` | 300 s | `TimeoutStartSec=1800` | ~04:35 |

**Neither overruns into 05:00**, including on Sunday when both run.
The dream's `RandomizedDelaySec=120` stays useful and costs 2 min out of 240 — noise.

---

## 8. Delivery order

v1 §12's principle is reused and it's the only guard-rail that matters: **every step
is shippable without a single night's behavior changing**, until the one that changes it on purpose.

| # | what lands | regime after merge |
|---|---|---|
| 1 | ~~The **four hardcoded prompt lines** + the missing `project_key` check in `promote_validate`~~ (v1 §3.5, §12 step 1) — **SHIPPED**, `ca13c1e9` + `89b5926d`, branch `fix/dream-lot1-prompt-scope` (§13) | unchanged (a single project), **proven** not promised |
| **R** | **REORG Part 2's scope**: the whitelist moves out of prose and into code (done, inert, `b917c0b7`), the filter gets wired into `brain_list`, Part 2 queries instead of paginating (§12.3, §12.4) | **changes on purpose**: REORG archives its 7 targets the following night, reversible |
| 2 | **042** applied in production, `alembic current` **proven**, then `dream_runs`'s 5 writers and 10 readers | unchanged, column filled |
| 3 | **Structural exit of the three global phases**, sentinel `'*'`, textual test anchor | unchanged (they already ran once) |
| 4 | Log paths given a project (7/12), export reset, `project/phase` counters, grouped aggregated alert | unchanged at one project |
| 5 | **`freshness_status_updated_at` + `freshness_source`** on the six tables, written by the four writers | unchanged; the clock starts ticking |
| 6a | **The candidate producer** (read-only), **with its deterministic offset** (§4.3 property 7), delivered **as a human-review report** — decoupled from REORG, conditioned on nothing (§4.3 property 6, option **d** + shape **c**) | one more report, no mutation |
| 6b | **The counter swap** + `last_accessed_at_human`, **behind a setting** (§5.5) | the only change with immediate effect on a human |
| 6c | The six `freq_baseline`s + REORG's guard moved to the human counter | **inert today** (§5.3: 51 of 2,343 entities); to ship once the counter has warmed up |
| 7 | **The cursor** + the 05:00-09:00 window + admission by time remaining, **window width = 1 project** | identical to today, byte for byte |
| 8 | **The server scope**: the five empty `DreamProjectToolPolicy()`s filled in, `BRAIN_DREAM_CAPABILITY_ENFORCEMENT` armed | **per-project** regime change, measurable |
| 9 | **Widening the window**, a few units at a time, measuring each night | progressive coverage, reversible by lowering a number |
| 10 | **Purge**, closed and dry, after several months of archive stay measured by step 5 | nothing gets deleted |

Four ordering reasons that aren't preferences:

- **5 before 6, and well before 10.** The status timestamp is the only way to measure a
  stay; shipped late, it pushes purge back by the same amount. It's the smallest and most
  blocking step.
- **6 before 7.** §4/§5 works **per entity** and produces an effect from the first night on
  `brain-v42` alone. It depends on neither the cursor, nor the scope, nor the window. Shipping
  it first banks the value before the expensive part.
- **6 is split into three, and the internal order matters.** The review measured that 6c is
  inert today (§5.3) and that 6a changed nothing as long as REORG Part 2's mandate
  wasn't decided (§4.3, property 6). Bundling them together meant shipping an immediate
  search effect (6b) under the same setting as a no-op (6c) and an open question
  (6a). **The split still holds, but for one fewer reason**: since §12.3, 6a is no longer an
  open question — it's a decoupled report. What remains is that 6b is the only one of the
  three a human would notice the same day, and that's what justifies it having its own setting.
- **R has no place in the ordering, and that's a fact, not a convenience.** It depends on
  neither 042, nor the archival clock, nor the cursor, nor the scope: it fixes a scan that
  was already broken at a single project. It carries a letter rather than a number because renumbering 2-10 would break the "step 5", "step 6", "step 8" references scattered throughout
  the document — a documentation risk with no upside.
- **8 before 9.** §1.3: opening the window before the scope means redoing the same global
  work 25 times a night. The loop ships (7), it doesn't open (9).

**This operator decision is made** (§12.3): option **(d)**, shape **(c)**. Batch 6a is
therefore specifiable, and it has settled into an ordinary report — it's batch **R** that now
carries the effect on REORG. The first draft said "batch 6a is only specifiable after";
that was true, and it's no longer a wait.

**The only arbitration still open on this table is arming R.** The code is green and
inert; arming it is two gestures (wire `brain_list`, rewrite Part 2) and the first merge
to `main` archives seven entities the following night. It's reversible — `freshness_status`
resets to `fresh` — but it's a real behavior change, and §12.4 recalls that the root is a
deployed working tree.

**Out-of-repo constraint, reused from v1 §12**: `dream.sh` runs from the **root** working
tree, so merging to `main` is deploying. 042 applies in production **before** the
merge that introduces its readers. And the revision number is **measured** every time.

---

## 9. What ships with closed killswitches

- **`BRAIN_DREAM_PURGE_ENABLED=false`, `BRAIN_DREAM_PURGE_DRY_RUN=true`**, absent from the
  drop-in, defaults in `dream.sh` on the `BRAIN_DREAM_SWEEP_*` idiom (`:58-59`). Nothing gets
  deleted before an explicit decision backed by a measured manifest.
- **The default window width is 1.** The cursor turns, serves one project, and the night
  is identical to today. This is the property that makes step 7 safe.
- **`sweep` doesn't move**: `ENABLED=false`, `DRY_RUN=true`, absent from the drop-in.
  Re-checked: it still has **zero rows** in `dream_runs`. Its soak is independent.
- **No existing killswitch changes value.** Not PROMOTE, not REORG, not EXTRACT, not
  ROADMAP. The live drop-in carries **7** active `Environment=` directives (§7.1: the first
  draft said 8, counting a comment) and this batch modifies none of them.
- **`BRAIN_DREAM_CAPABILITY_ENFORCEMENT` stays absent** until step 8, and arming it is
  a batch of its own.
- **The decay recalibration ships behind a setting**, today's values as defaults (§5.5).
  It's the only element with no irreversibility but an immediate effect on a human.
- **What does NOT need to be closed, and why**: the candidate producer (§4.3) is a
  `SELECT`. `freshness_status_updated_at` is a column with no backfill — `NULL` means
  "never measured", the 040/041 doctrine. Neither can break anything by being open,
  and closing them would delay the clock purge depends on.
- **Batch R has no killswitch, and won't get one — because none would be useful.**
  REORG is **WET** today; its killswitch protects the entire phase, not a scope
  change inside it. Adding a flag specific to R would give the illusion of a guard when
  the real guard is elsewhere: **REORG only does what its prompt tells it**. As long as
  `phase_reorg.md` paginates, `pollution_patterns.py`'s capability is inert, and this is
  *verifiable* — `git grep` on its callers, not a boolean to trust. R's switch is
  therefore the prompt itself, and arming it is a merge, not a flip.
- **042 is not a killswitch**: nullable and without backfill precisely so a
  non-migrated reader and a migrated writer can coexist.

---

## 10. Accepted limits

- **The server scope is off, so the loop multiplies the work without multiplying
  coverage** (§1.3). Opened before step 8, it would do 25 times a night what it does
  once today. This is the whole project's dominant limit.
- **No night with more than one project has ever run.** All of §7's sizing is
  simulations over real single-project nights. Unchanged since v1.
- **The red-night rate becomes a non-signal.** v1 §9 measures 6.7% (30 nights) and 16.5%
  (121 nights) of nights with at least one non-`done` agent row. Under the same
  independence assumption — false in the conservative direction — 14 projects a night give
  `1 − (1−p)^14` = **62%** at the recent rate and **92%** at the historical rate. A red unit
  two nights out of three no longer says anything. v1 §9's conclusion holds and hardens:
  **the useful signal is the report's content grouped by project, never the unit's color.**
- **The "human" counter counts non-dream reads, not human reads** (§3.4),
  and `access_log` keeps no history to verify it.
- **`freq_baseline` and the thresholds aren't calibrated** in `thresholds.py`'s sense
  (`calibrated=False`). The six values in §5.3 come from a **two-day** distribution —
  and, measured, they only change the multiplier for **51 of 2,343 entities** today.
- **The score → REORG link has no recipient** — and since §12.3, it won't get one: the
  coupling is refused, not deferred. Zero of the 500 lowest multipliers match the whitelist,
  and §12.2 gives the underlying reason — the two mechanisms target opposite populations by
  construction. This is therefore no longer "§4's dominant limit" awaiting a ruling:
  it's a **non-limit**, §4 simply doesn't lead into REORG. What remains open is
  narrower and clearly named: **who the ranking is addressed to**, if not REORG
  (§12.6). The disabled scope, though, stays §2's dominant limit.
- **The candidate producer has no specified rotation mechanism** (§4.3, property 7):
  without it, `limit=20` is a frozen window over 2,343 entities.
- **`decay.py`'s archival threshold is unusable on this corpus** (minimum 0.2228 > 0.2) and
  the spec works around it with a rank rather than retouching it. An older corpus will make
  it usable; no one will notice automatically.
- **The candidate producer will cost one computation per entity and per project**, not
  measured: the formula is pure and I/O-free (`decay.py`), but loading the rows isn't.
- **Empty projects are skipped on an inference** (§2.8), not on an operator
  decision.
- **The never-served cap comes from a single month** (March 2026, 25 creations). One
  observation — and its value was corrected twice by the review (§2.5).
- **No per-project cap finer than the one that already exists** (§2.7): the structural
  cap is 53 min without retry / 96 min with, i.e. 47% of the loop; it's the **empirical
  distribution** that's missing, not the cap.
- **Log rotation is still absent** (1,865 files / 121 MB, v1 §16) and the widened window
  brings that deadline closer.
- **`dream_runs` has no project column today**, so all history before 042
  is permanently unattributable. v1 said so for telemetry; §4.2 shows it also holds
  for the archival audit.

### What I could not measure

1. **The throughput of the subscription that authenticates Codex.** v1 §17.1 had already
   named it as the most serious unquantified risk; at 14-25 projects a night it grows accordingly.
   ~11.6M tokens/night at 8 projects was already an extrapolation.
2. **The real duration of an agent run scoped to a non-`brain-v42` project.** Never run. The
   15-min admission budget (§7.3) comes from **unscoped** nights on **one** project; it's
   the best estimate available and it will be wrong on step 8's day.
3. **The real runtime behavior of the `DecayFlusher` path.** §4.1's conclusions are
   arithmetic (`decay.py` constants) and corroborated by three `stale` snippets, not by
   an instrumented run. The review did, however, run the real `DecayCalculator` on the
   corpus (§0): it's the **formula** that's now measured, not the **path** that calls it.
   Further corroboration of the 0.44 floor: `freshness_status='stale'` exists **only**
   on snippets (3 rows) and on no other table — measured across all six.
4. **Actor identity.** `access_log`: 0 rows. Neither the human/machine mix, nor the
   number of distinct actors, nor the validity of the `dream-codex-` prefix as the only
   system marker.
5. **The Neo4j cost** of a purge or a candidate pass, and the number of orphan nodes
   the graph already carries.
6. **The 2026-06-30 archival on `red-shrik:agent`**: the window coincidence is a
   bundle of clues (§4.2), not a trace. No audit data can settle it, and there
   never will be one for the past.
7. **Out-of-repo consumers of the `/metrics` payload** (red-monitor) versus the
   `DISTINCT ON (phase)` readers: not inspected, same as in v1.
8. **`record-empty-pool`'s behavior**, which had still written **no** row at all as
   of v1 §10. Not re-checked today.
9. **The per-project duration distribution**, which doesn't exist: `dream_runs` has no
   project column. All of §7 reasons about the duration of a **night**, not a project.

### Out of scope

- **Prefix semantics** (`red-lab` seeing `red-lab:architect`). D1 makes the six two-tier
  keys eligible as full-fledged projects; **aggregating** a parent and its children
  remains a project with no mechanics in the code at all (v1 §2: `promote_prepare.py`
  filters by exact equality).
- **Opening `sweep` in WET** — an independent decision, never in the same batch.
- **Binding the 19 keyed artifacts with no `project_context`**: the
  `2026-07-27-orphan-project-key-bind-design.md` family. (The "26 artifacts without
  `project_key`" from the first draft do not exist — see §2.1's box.)
- **`scripts/dream/cross_project_resonance.py`** — no call site (v1 §18).
- **The project in `X-Brain-Agent`** — blocked by `_MAX_AGENTS = 32` (v1 §14.2).
- **Log rotation**, pre-existing.
- **The red-monitor panel** and any out-of-repo display.

---

## 11. Adversarial review of 2026-08-08 (03:24 UTC)

Every figure in the first draft was replayed against production, except those
explicitly marked "v1 §N". The commands are the ones cited in the document's body.

### 11.1 What held, unchanged

`alembic current` = **041**. 55 `project_contexts` · 9 orphan keys / 19 artifacts · 9 empty
contexts · monthly creation rate (6 · 8 · 25 · 2 · 1 · 3 · 7 · 3). Agent durations
**9.28 / 14.73 / 30.50** and global **5.77 / 42.26**, over exactly 30 nights. Eight phases in
`dream_runs`, still no `sweep`. 363 sessions across 17 projects, `RESTRICT` FK. 1,180 never
read · 2,522 machine-only. §5.1's full table, line by line. 1,779 / 51 / 912. All of
§4.2: 348 tombstones, 11 judgments, 10 on `brain-v42`, 1 on `red-shrik:agent` at
`2026-06-30 04:20:57`, inside the REORG window `04:15:41 → 04:21:47` — and the subject
`test_phase2_red-shrik` does match the whitelist's `^test_`. All of §5.4, recomputed by the
**real** `DecayCalculator`. All of §6.2: 0 candidates everywhere, 98 / 2 / 0 / 1 / 0 pending,
deadline 2026-09-09. 105 rows in `dream_promotions`. All systemd values and **all**
cited line numbers (`dream.sh:20, 58-59, 106-113, 116-123, 196-197, 260, 351, 439, 447, 455,
469, 501, 504-510, 522, 539, 664, 706, 735`; the four hardcoded prompt lines; the five
empty `DreamProjectToolPolicy()`s; `install.sh` counting only `Environment=`). §10's
probabilities (62% / 92%).

**And the review's central question is settled in the spec's favor**: human priority
never turns back into a filter. Simulated over the 55 real projects — 44 at zero
priority — no project is ever starved, in any of the five configurations tested (§2.4).

### 11.2 What changed

| § | what was written | what is measured |
|---|---|---|
| **2.1, 10** | "26 artifacts without `project_key`", in the "measured value" column | **0**, across the five tables and `indexed_plans`. A figure inherited from v1, never replayed |
| **4.3 (5)** | the `access_count > 5` veto "cancels the nominator" | **empty** intersection at rank 20 and rank 100; the veto only bites from rank 264 |
| **4.3 (6)** | nothing | **the whitelist in `phase_reorg.md:64-71` is the real gate, and it closes 100% of the time**: 0 of the 500 lowest multipliers match any of the six patterns. The link is a no-op as long as Part 2's mandate isn't decided |
| **4.3 (7)** | nothing | the producer is itself a ratchet: frozen order, rejections not memorized, `limit=20` frozen over 2,343 entities |
| **4.3, 6.6** | "**no** honest clock exists" | `brain_entities.updated_at`, via a trigger that **excludes** `access_count`, is one — imperfect but immune to the flusher |
| **4.3** | "the 040/041 doctrine" | a choice must be made: it's **041** (conditional trigger), because `freshness_status` has four writers, one of which is a prompt |
| **5.3** | six baselines presented as the recalibration's core | **inert**: 51 of 2,343 entities change, `stale`/min/p10 identical. The entire effect comes from the counter swap |
| **6.2** | "98 learnings that follow" on 2026-09-09 | **97 of the 98 are merge tombstones**; excluding tombstones, the blast radius is **1** |
| **6.3** | 31 keys / 567 artifacts | **32 / 580** twenty minutes later — the radius shifts visibly |
| **6.5** | one structural guard (`brain_sessions`), three couplings | **two** guards (`brain_entities → projects` RESTRICT blocks `projects`'s 74 rows), **four** `SET NULL` FKs from `dream_promotions` |
| **7.3** | an **absolute** deadline "e.g. 08:55" | incompatible with `Persistent=true`, measured in the same place: a late catch-up admits **zero** projects, silently |
| **7.3 / 2.5** | coverage 2.5 / 4.0 / 8.2 nights | computed **without** the never-served cap; with a fixed cap, 8 nights at p90. §2.5 corrected to make both compatible |
| **2.5** | "~12 out of ~25", "absorbs March in two nights" | 12 absorbs 25 in **three** nights; and the window §7.3 plans on is **14**, whose half is **7** |
| **2.7** | "no data to calibrate a per-project cap" | the cap exists: **53 min** without retry, **96 min** with, i.e. 47% of the loop. It's the distribution that's missing |
| **1.1** | "at 55 projects, 3,050 min" | **2,950** (55 × 53 + 35) |
| **7.1, 9** | "`killswitches.conf` 8 `Environment=`" | **7**; the unanchored pattern counted a comment |
| **5.4** | human p10 0.2906 | **0.2905** (real code vs. SQL replica) |
| **0** | caveat: "the Python wasn't run" | **lifted** — the real `DecayCalculator` was run, and it agrees |

### 11.3 What the review could not do

- **Run a night.** No multi-project loop, no cursor, no candidate producer
  exists. Everything touching nightly behavior remains a simulation on measured
  durations of **single-project** nights.
- **Check what an LLM judge would do** with a list of candidates ranked by the score. §4.3's
  property 6 measures what the **prompt** forbids, not what the model would decide if
  allowed to.
- **Settle the 2026-06-30 archival.** `access_log` stays at 0 rows; the bundle of clues
  stays a bundle of clues.

---

## 12. Measurement pass of 2026-08-08 (11:00 UTC) — the recipient is decided

§5.4 ended on "The ranking is good; the recipient is still to be chosen." This pass
chooses, and the measurement behind it shifts the problem: **§4.3's property 6 was true but
too narrow.**

### 12.1 What property 6 didn't say

The 03:24 review measured "0 of the 500 lowest multipliers match one of the six patterns."
Exact, re-checked. But asking the question the other way around yields a heavier fact —
measured over the 3,184 entities neither archived nor merged (2,345 learnings + 839
decisions), regexes applied as-is and the real `DecayCalculator`:

| | measurement |
|---|---|
| living whitelist targets, **whole corpus** | **7** |
| their ranks by `created_at DESC` | **587, 920, 921, 923, 925, 926, 927** |
| window `phase_reorg.md` Part 1 paginates | first **500**, out of 2,345 learnings |
| `reorg` runs `done` since 2026-04-06 | **112** |
| learnings archived **outside** a merge tombstone, whole corpus | **1** |

**REORG Part 2 has never been able to reach its own targets.** It isn't the candidate
producer that runs into the whitelist: the phase, alone and since day one, paginates the 500
most-recently-created learnings, and its seven targets were created on 2026-04-17. The gap
widens mechanically with every new learning.

The corpus's one non-tombstone archival is very likely the 2026-06-30 one already
covered in §4.2 and §11.1 — `test_phase2_red-shrik`, inside the REORG window `04:15:41 →
04:21:47`. In other words, Part 2 has bitten **once in 112 nights**, and §11.3's bundle of clues
gains a figure here: it isn't "we don't know if REORG did it", it's "if REORG did it, it's its only catch."

### 12.2 The two mechanisms select opposite populations

This is the underlying reason, and it rules out the obvious fix. Ranks of the seven targets
**by decay**, out of 3,184: **948, 1318, 1320, 1321, 1359, 1401, 2115**, scores 0.35 to 0.57.
They sit in the middle and top of the ranking, not the bottom — because the pollution
targeted is **recent** machine output (`red-shrik:agent`, last artifact 36 days ago), and
decay scores recency.

The bottom of the ranking, by contrast, isn't pollution: it's the full corpus of finished
projects. `second-cerveau` (215 d), `lyriks` (213 d), `datalake-v2` (167 d), `poc-lyriks-v2` (165 d).

Widening the whitelist would therefore not "fix" the producer — the two sets don't
overlap by construction, and no reasonable widening will make them overlap. It would
change what REORG **is**, exactly as §5.4 feared.

### 12.3 Decided: fix the SCOPE, not the width

Operator decision of 2026-08-08. The whitelist keeps its six patterns. What changes is the
**scan's scope**: an exact query on the patterns, instead of a 500-row window sorted
by a column unrelated to them.
Consequences for this spec:

- The coupling "candidate producer → REORG Part 2" is **refused**, not deferred. The
  decay ranking and the whitelist don't target the same population; wiring one into the other would be a non sequitur regardless of the setting.
- The ranking's recipient therefore still needs to be written, but it's no longer
  *blocking*: REORG becomes operative again without it. The two efforts separate cleanly.
- The bottom of the ranking is a **project** signal, not an entity one — 9 entities for
  `lyriks`, 3 for `second-cerveau`. An open lead, not settled here: one human decision
  per finished project rather than 500 LLM verdicts per entity. It joins purge's entry
  ticket from §6.4.

### 12.4 Shipped inert — `b917c0b7`, branch `fix/reorg-scope-pollution-candidates`

`src/brain_v42/services/pollution_patterns.py` moves the six patterns out of prose and pins
them with a test. The SQL clause is **derived from the same tuple**, so drift between the
Python matcher and the query is impossible by construction.

**Correction to this section's first draft.** It claimed "no test backed by the
database: `BRAIN_V42_TEST_DB_URL` isn't set anywhere and ticket `71576155` records that
these tests never run in CI." Both halves were wrong. Measured since: **both**
rails set the variable on the **unit** job — `.gitlab-ci.yml:131` and
`.github/workflows/continuous-integration.yml:98` — plus an `alembic upgrade head`, because
`tests/unit/` has no fixture that creates the schema. This is ticket `71576155`'s fix, which is
therefore **validated**: `test_promote_prepare.py` returns 23/23 with the variable set, and the
CI comment carries the measurement of the closed gap — 7,249/54 without, 7,294/9 with, i.e. 45
tests that had never run. The ticket can be closed.

The missing test is therefore written (`test_pollution_clause_pg.py`, `a3bd7ab9`). It covers
what deriving from a single tuple doesn't cover: **the two regex engines must agree**.
Python compiles with `re.IGNORECASE`, PostgreSQL receives the same string via `~*` in its ARE;
whether `\d`, `.*`, `^` and `$` mean the same thing on both sides is an empirical question about
PostgreSQL, not a design choice. This is **not** the SQLite mirror-table pattern (brain snippet
`4b86f802`): `~*` is a PostgreSQL operator SQLite would not parse, so a mirror would test
nothing here.

The tests do bite, verified by mutation: `~*` → `~` fails 3 tests, dropping the `$` from
`_events_\d+h$` fails 2 here and 2 in the pure tests.

The empirical measurement against the real corpus stays true and keeps its own value — a test
runs against a constructed set, this runs against production: over 2,744 learnings and 844
decisions, SQL and Python select identical sets (54 and 0).

Every anchor carries its negative twin, per the focus's methodological rule: dropping the `^`
from `^test_` makes it match `my_test_helper`; dropping the `$` from `_events_\d+h$` makes it
match `count_events_24hours`.

**Nothing calls this module and `phase_reorg.md` isn't touched.** Nightly behavior is
therefore unchanged, and that's *verifiable* rather than promised by a flag: here the
prompt **is** the switch, since REORG only does what its prompt tells it. Arming it will be
a separate gesture — wire the filter into `brain_list`, then rewrite Part 2 so it queries
instead of paginating. The day this reaches `main`, REORG archives its seven targets the
following night; it's reversible, but the root is a deployed working tree.

Gates, exit codes read directly and not behind a `tail`: `ruff check` 0,
`ruff format --check` 0, `mypy` 0. Unit suite **in the CI configuration**, base variable
set to `brain_test` — never `brain`, which `tests/integration/conftest.py`'s guard
rejects by name: **7,345 passed, 9 skipped**. The 9 skips match exactly the figure the CI
comment recorded, which counts as cross-checking. Without the variable, the same branch
returns **7,294 passed / 60 skipped**: this batch's 51 tests run, but the 4 new PostgreSQL
tests join the 56 already skipping. That's the regime the first draft described — a local
run, not what CI does.

### 12.5 A new fact touching §6.5

**41 of 395 merge tombstones (10%) still carry `freshness_status='fresh'`** — 40
learnings, 1 decision, against 348 + 4 correctly archived. They're hidden from `brain_list`
by `pg_base.py:494`, which adds `merged_into IS NULL` when `include_archived=False` — a
clause **independent** of their freshness.

This is exactly the trap §6.5 names ("any rule must exclude `merged_into IS NOT NULL`, or
say why it doesn't"): a rule filtering on `freshness_status` alone counts them as
living knowledge, while the tool itself never shows them. CLEAN's inconsistency,
not v2's; noted here, not handled.

### 12.6 What this pass did not do

- **Still no night run.** The "7 targets" figure is a corpus state as of 2026-08-08
  11:00, not a nightly throughput.
- **The wiring doesn't exist.** `brain_list` has no filter on the patterns; until it
  exists, "the scope is fixed" describes a capability, not a behavior.
- **The decay ranking's recipient still needs to be written**, along with §12.3's project
  question.
- **The 2026-06-30 archival is still not settled** — `access_log` stays at 0 rows. This
  pass only bounded it above: at most one catch in 112 nights.

---

## 13. Batch 1 — shipped on 2026-08-08

Branch `fix/dream-lot1-prompt-scope`, two commits, not merged. This batch is written here
**after** shipping, which is the wrong order: the two design decisions below were
made in conversation then coded, without going through this document. They're here now,
and this spec is the source of truth.

### 13.1 The four prompt lines — `ca13c1e9`

Three of the four named `brain-v42` in a **scope** position: `phase_promote.md:4`,
`phase_synth.md:24` and `:58`. Rendered for another project, they tell the agent to write
into brain-v42 — SYNTH even ordered it as a guard-rail ("Always use `project_key="brain-v42"`").

**The fourth isn't one.** `phase_connect.md:43` lists `brain-v42` in a domain-taxonomy
example ("memory — knowledge graph, brain-v42, embeddings, …"). Templating it would rewrite
the taxonomy per project instead of naming a subject. It stays, on the whitelist **named**
in the test, with its reason — and a test checks it's exactly that line, not another
one that drifted underneath. §1.1's count of "four" was therefore accurate but blended
two natures.

**Decision: preserve byte-for-byte rendering rather than improve the wording.** "v1 is
brain-v42 only" becomes "v1 is `{{PROJECT_KEY}}` only" rather than a cleaner phrasing,
because rewording changes an LLM's prompt and no rewording is provably neutral.
This is what makes the batch's inertness **verifiable**: the six prompts rendered for
`PROJECT_KEY=brain-v42` are identical before and after (`diff -r` on the six outputs, exit 0).
Cleaning up this phrasing is a separate batch, and it will have to own itself as a prompt change.

### 13.2 The validator's scope check — `89b5926d`

`promote_validate.py` didn't look at `project_key`. At 55 projects, PROMOTE can produce an
ADR or a runbook under the wrong project with nothing downstream ever seeing it.

**Operator decision: hard failure, not logging.** A mislabeled promotion is a
referential-integrity violation on the same footing as the file's other ones — it raises
`ValidationFailure`, and `main()` marks `dream_runs` as `partial` via the handler that
already exists. The alternative — logging without failing — would produce a signal no one
reads, which §11.3 already calls out about the 2026-06-30 bundle of clues.

**`--project-key` is required, with no default.** A `brain-v42` default would
silently validate every project against the wrong scope the day the loop
opens: exactly the class of bug this argument exists to catch. `dream.sh:574`
passes `$PROJECT_KEY`.

**Inert today**, like the rest of the batch: at one project, the source learning and the
produced entity carry the same key, so the check passes trivially. It only bites once the loop opens.

### 13.3 What makes these tests non-decorative

Both halves carry negative twins, per the methodological rule: a test asserting only a
presence can't testify to a filter's removal. Dropping the `^` from `^test_` makes it
match `my_test_helper`; a scope check that refused **everything** would look correct
without its positive twin "same shape, right project, passes".

And the checks were proven by **mutation**: neutralizing the ADR comparison fails
its rejection test, neutralizing the runbook one fails its. Without this step there's no
way to know whether a test covers the line or just executes it (brain snippet `4b86f802`).

### 13.4 What this batch does not do

- **It opens no loop.** The phases are still invoked once, on `brain-v42`.
- **It doesn't touch `phase_connect.md`**, by decision, not by omission (§13.1).
- **It doesn't check the *source* learning's key**, only the produced entity's. Measured
  rather than assumed: this has no consequence, because the producer is **already** scoped — `fetch_candidates(session_factory, project_key, limit)` (`promote_prepare.py:84`),
  fed by `dream.sh:462` with the same `$PROJECT_KEY` now going to the validator
  (`dream.sh:574`). The promote phase therefore reads and writes under one and the same
  key, end to end. What remains uncovered is narrow: a candidate already mislabeled
  **in the database** would pass both guards, since they agree on the key without
  ever questioning it.

---

## 14. Batch 2 — 042, made executable

Written **before** the code, unlike batch 1. The column's shape was already settled (§1.2)
and so was its role (§2.3: telemetry, never scheduling). What was missing to execute
was the real inventory of sites to touch and the out-of-band manoeuvre. Everything below
is measured on 2026-08-08, not copied forward.

### 14.1 The starting state, re-measured

`dream_runs` carries **16 columns**, none of them a project. Only two indexes:
`dream_runs_pkey(id)` and `idx_dream_runs_date(run_date DESC)`. `alembic_version` = **041** in
production as on `brain_test`, and the latest file in `alembic/versions/` is
`041_corpus_provenance.py`: **042 does not exist yet**, neither in the database nor on disk.

Eight phases present — `connect` 136, `clean` 136, `scan` 136, `synth` 135, `reorg` 116,
`promote` 112, `roadmap` 52, `extract` 41 — and still **no `sweep`**, which reconfirms §1.2.

042 adds `project_key VARCHAR(64) NULL`, with no backfill, plus the index `(run_date DESC,
project_key)`. `NULL` means "row written before 042", forever; `'*'` is the
sentinel for global phases.

### 14.2 The inventory, and the distinction "5 writers / 10 readers" doesn't make

> **This table is wrong by one row, and §15 is the source of truth.** The 2026-08-09 pass
> measured **six** `INSERT`s, not five: `scripts/dream/_promote_helpers.py:125` is missing,
> which this section otherwise files among the **readers**. It's alive and has written the
> `promote` rows of the last two nights. The `SELECT` list is also undercounted (§15.6).
> **Don't treat this table as the work list** — §15.3 carries that.

§8's count is accurate but it only talks about `INSERT`s. Measured, there are **nine**
write sites, of two natures:

**Five `INSERT`s — the ones that must carry `project_key`:**

| site | phase written | alive? | failure | key to set |
|---|---|---|---|---|
| `metrics/dream_parser.py:181` | parameterized | **yes — the only one writing all six phases per project** | raises, but `dream.sh:338` swallows it as `WARN … (non-fatal)` | the real key |
| `scripts/ticket_extract.py:752` | `extract` | yes, 41 rows | swallows — "Best-effort — never raises" | `'*'` |
| `scripts/roadmap_curate.py:1146` | `roadmap` | yes, 52 rows | swallows — same docstring | `'*'` |
| `maintenance/session_sweep.py:94` | `sweep` | shipped but **disarmed, 0 rows** | swallows — "the trace must never kill the phase" | `'*'` |
| `dream/cross_project_resonance.py:163` | `RESONANCE` | **dead** — 0 rows, no callers | raises | `'*'`, without wiring it up |

**Two corrections to this table's first draft, measured on 2026-08-09.**

First, `cross_project_resonance` **is not a live writer**. Its constant is
`PHASE = "RESONANCE"` and `dream_runs` has never carried a row with that name; a `grep`
across the whole repo finds no caller at all — not `dream.sh`, not a unit, not another
script. Only its own tests and one `thresholds.py` entry mention it. The first draft filed
it among the writers "that raise", giving it an importance it doesn't have.

Second, and this is the point that matters: **the "three swallow, two raise" split was
wrong.** `dream_parser` does raise in Python, but `dream.sh:338` catches and logs
`WARN dream_parser failed for $name (non-fatal)`. So **all five lose their trace silently**;
what differs is where the exception is swallowed — inside the function or inside the
orchestrator — not whether it is.

**Decided on `cross_project_resonance` (2026-08-09): it gets `'*'` without being wired up.**
Three lines, and it keeps it from being the one inconsistent writer the day someone
reconnects it. Wiring it up or removing it are two other decisions, out of this batch's scope.

**What the batch really weighs**, once this table is read correctly: **one** writer that
matters today, **two** live sentinels, **one** dormant sentinel, **one** dead one being
made consistent. "Five writers" overstates the work.

**Four `UPDATE`s — the ones that must NOT be touched**, named here so no one "completes"
them by symmetry: `cross_project_resonance.py:181`, `reorg_validate.py:271`,
`promote_validate.py:192`, `connect_validate.py:110`. They target `WHERE id = :id` and only
set `status`, `duration_s` and `error_message`. The row already exists, its key too;
rewriting it would be an opportunity to contradict it.

**Ten `SELECT`s, across seven files**: `collector_dream.py` (:90, :161, :175), `collector_nightly.py`
(:84), `_promote_helpers.py` (:91), `post_run_alert.py` (:115), `dream.sh` (:612),
`dream_preflight.py` (:107), `connect_validate.py` (:100). None filters by project today —
which is what keeps the column inert until they're touched, hence safe to ship before them.

### 14.3 Why `NULL` isn't caution, but a measured consequence

The table above gives the reason, and it's stronger than "we're avoiding a backfill":
**all five `INSERT`s lose their trace silently.** Three swallow it in their own
function, written into their docstrings; the fourth — `dream_parser`, the one carrying all
the per-project telemetry — raises, but its orchestrator catches it (`dream.sh:338`,
`WARN … (non-fatal)`). A `NOT NULL` column would therefore turn a schema error into a
warning printed **everywhere**, not on three sites — the night would lose its trace with
no sound at all, which is the defect this spec is trying to remove, not relocate.

> The first draft said "three of the five", and drew a tempting alignment from it: the
> three best-effort ones would have been exactly the three global phases. That was true by
> accident and wrong in substance — the argument is **stronger** without it, because it
> covers all five. An elegance resting on a headcount is an elegance to re-check.

**Who writes what**, without seeking symmetry: `dream_parser` gets the real key, because
it's invoked once per phase and per project (`dream.sh:334`). The other four write the
sentinel `'*'` — `extract`, `roadmap` and `sweep` because they're §1.2's global phases and
a global phase has no project to name; `RESONANCE` because it's dead and is being made
consistent rather than left alone and inconsistent.

### 14.4 The out-of-band manoeuvre

Constraint reused from §8 and v1 §12: `dream.sh` runs from the **root** working tree, so
merging to `main` is deploying. 042 applies in production **before** the
merge that introduces its readers. Sequence:

1. Write `042_dream_runs_project_key.py` on a branch, in a worktree — which doesn't deploy.
2. Apply it first on `brain_test`, the only database where a round trip has no consequence.
   Check both `upgrade` **and** `downgrade` on that database.
3. Apply it in production **from the worktree**, not from the root.
4. **Prove** the revision, don't assume it:
   `docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"`
   must return `042`. CLAUDE.md carries the warning that justifies this step: this line
   claimed "production stays at 037" for **three days** after a cutover.
5. Only then, merge the five writers.
6. The ten readers come after, in their own batch — they have no reason to share the
   migration's fate.

Between 4 and 5, production runs with a column nobody writes: this is the intended state,
and it's what §9 calls "042 is not a killswitch".

> **This sequence is incomplete — see §14.7.** It was written before the migration
> existed, and it's missing the head-pin step, discovered while coding it. §14.7 carries
> the corrected version, which is the source of truth.

### 14.5 The downgrade

Measured precedent: 041 settles for `op.drop_column`, with no fail-closed guard — that's
reserved for migrations whose reversal would lose a state that can't be rebuilt (037 for
session captures). 042 is telemetry with no backfill: its downgrade is a
`drop_column` plus removing the index, and it loses the keys written since the upgrade.

**Decided on 2026-08-08: like 041, a plain `drop_column`, with no fail-closed guard.** The
loss is real and accepted — a `dream_runs` with no project key is exactly the prior state,
which held up for 135 nights. A guard would only make sense if the lost data were
irreplaceable; here it's reconstructible from one night of telemetry, and §2.3 already
forbids anything important from depending on it. Refusing the downgrade would instead make
the rollback harder than the rollout, which is the wrong direction for a telemetry migration.

### 14.6 What this batch does not do

- **It opens no loop** and creates no cursor. §2.3 explicitly forbids deriving
  scheduling from this column, and §14.2's table gives the quantified proof: three of
  five writers can write nothing without anyone knowing.
- **It doesn't fill in the past.** The 864 existing rows will stay `NULL`. Any
  retrospective per-project measurement is therefore permanently impossible — that's a
  price already paid, not a new one.
- **It doesn't touch the ten readers.** As long as they don't filter, the column is dead
  data, and that's precisely what makes step 5 safe.

### 14.7 The step §14.4 had forgotten: the six head pins

Discovered while writing the migration, not while designing it: **the repo refuses an
undocumented head bump, via six separate tests.** §14.4's manoeuvre is therefore
incomplete: it's missing a step, and it's the most important one, because it's the one
that prevents the failure CLAUDE.md recounts.

**And the inventory can't be taken in one pass — that's this section's methodological
point.** Placing `042` on disk fails **five** tests (measured: 5 failures out of 7,299). Fixing those five surfaces a **sixth**, which couldn't bite before: it doesn't pin the
head, it pins a *sentence in `SCHEMA.md`*, and that sentence hadn't moved yet. An
inventory built on "what breaks when I add the migration" is therefore structurally
incomplete. The only reliable measurement is iterative: fix, rerun the whole suite,
repeat until green.

| Pin | What it requires |
|---|---|
| `test_alembic_cli_fail_closed.py:143` | the **offline** render of every migration counts `Running upgrade`: `41` → `42` |
| `test_documentation_contract.py:1755` | `docs/SCHEMA.md`: "41 revisions (001 → 041)", a `\| 042 \|` row in the table, and the table count at head |
| `test_documentation_contract.py:1778` | `migrations 001–042 defined` in `ARCHITECTURE`, and `migration 042` in `README`, `CLAUDE.md` and `MCP_TOOLS` |
| `test_plan_index_repair_head_pin.py:45` | an **explicit review** before any bump of `_REQUIRED_ALEMBIC_HEAD` |
| `test_recovery_contract.py:271` | `script.get_heads() == ["041"]` — a marker that "this contract was reviewed at this head" |
| `test_recovery_contract_v4.py:436` | "The repo's target is 041." in `SCHEMA.md` — **duplicate**, and the only one that bites only on the second pass |

**Why six and not one.** Each guards something different: that migrations render
offline with no secret, that the documented schema exists, that four entry pages agree,
that the index repair was re-reviewed, that the recovery contract was re-read. A single
global pin would have been simpler and would have let five of the six questions through.

**The sixth is misplaced, and it must be said.** It lives inside a test named
`test_full_runbook_has_one_039_operator_order_and_no_live_claim` — a test about 039's operator order — and asserts, in passing, a `SCHEMA.md` sentence
`test_documentation_contract.py:1764` already asserts. A duplicate hidden inside a test
about something else is invisible to anyone inventorying the guards, and that's exactly
what made me write "five". This isn't a flaw in the guard — it does its job — but in
its placement.

**And the second one's docstring says exactly what this spec spends its time repeating:**

> "Until 2026-08-04 these docs asserted a production head of `037` while the database had been on
> `039` for three days. No page can prove a live head, so the docs now name the repository target
> and send the reader to measure the rest."

This is the distinction never to lose: **the repo owns the repo's head, it does not own
the deployed head.** Documents name a target; only `psql` states the actual state. The test's name contains `041`, so bumping requires renaming it — the repo makes its own
marker impossible to forget.

**The review the `plan_index_repair` pin requires, done and logged here.** Its message
asks to check what the new head changes on the three tables the repair writes —
`indexed_plans`, `indexed_plan_chunks`, `project_contexts` — looking for triggers,
constraints or `NOT NULL` columns with no default. Measured: 042 touches **only**
`dream_runs`, adds **no** trigger, **no** constraint, and its only column is nullable with
no default. The repair's three tables are untouched. **The bump is safe**, and it's the
only one of the five pins that called for judgment rather than a count.

**A figure that doesn't move, and it's worth saying**: a fresh schema has **32 `public`
tables** at head 041 as at head 042 — measured on `brain` and on `brain_test`. 042 adds a
column, not a table, so `SCHEMA.md`'s sentence only changes its number.

**The corrected manoeuvre.** The missing step slots in before any contact with
production, because it's code and documentation, not an operation:

1. Write the migration in a worktree.
2. **Update the six pins and the five documents, in the same commit as the
   migration.** A branch carrying `042` without its pins is red by design.
3. Prove `upgrade` **and** `downgrade` on `brain_test`.
4. Apply in production, from the worktree.
5. **Prove** the revision via `psql`.
6. Merge the writers; the readers in their own batch.

### 14.8 The seventh guard, and the price of the chosen order

**The seventh only surfaces when targeting production.** `alembic/env.py:70` refuses the
`brain` database without `BRAIN_ALEMBIC_ALLOW_PROD=1|true|yes`. The other six are static
and fail in the test suite; this one is at runtime, so **no test inventory could have
caught it** — it adds to the methodological point above, in another form. It's learning
`a567d2a8` ("alembic env.py silently migrated prod") turned into fail-closed.

**And the "migration before merge" order has a price, measured.** Between step 5 and
step 6, the deployed code still pins `041` while the database says `042`. And `plan_index_repair_store.py:230` compares the two and raises
`RepairSafetyError("alembic_head_mismatch")` on divergence. **Index repair is therefore
disarmed until the branch is merged.**

This isn't breakage, it's the guard doing its job — and the window is bounded, known,
and closes itself on merge. Two reasons to accept it rather than reversing the order:
`grep` over `dream.sh` and `scripts/dream/` finds **no** call to this path, so nothing
nightly depends on it; and reversing the order — merging first — would put into
production code that writes an absent column, which breaks for real instead of refusing.

**To tell the operator at the moment of applying, not after**: during this window, an
index repair will refuse to start. If it becomes necessary before the merge, the merge
is the remedy, not a way around the guard.

---

## 15. Batch 3 — the writers. Inventory pass of 2026-08-09

042 is applied and merged (`ecf7cf84`, `f94d078a`), production and `brain_test` are
measured at `042`, and **872 rows carry `project_key IS NULL`** — no writer sets it. This
batch is the one that fills it in. It's specified here **before** its code, and the
measurement pass that follows preceded the first line written.

### 15.1 §14.2's table is one row short, and that row is alive

**There are SIX `INSERT` sites into `dream_runs`, not five.** The sixth is
`scripts/dream/_promote_helpers.py:125`, function `_record_empty_pool`, an
`sa.insert(dream_runs).values(...)` — the only write in the batch that isn't textual SQL.
`git grep -nE 'sa\.insert\(dream_runs|INSERT INTO dream_runs' -- src/ scripts/` returns six;
no `COPY`, no `executemany`, no `ON CONFLICT`.

**The omission isn't a missed row: it's active.** §14.2 files `_promote_helpers.py`
among the **readers** ("`_promote_helpers.py` (:91)" in the list of ten `SELECT`s). The
file was therefore read, classified, and classified on the wrong side. An inventory
naming a file doesn't prove it understood it.

**And this sixth writer isn't dormant — it writes tonight's nights.** Measured:

```
select phase,status,count(*),min(run_date),max(run_date) from dream_runs
where error_message like 'empty candidate pool%' group by 1,2;
→ promote|done|2|2026-08-08|2026-08-09
```

The database's last two `promote` rows (ids 906 and 914) carry `model IS NULL` and
`EMPTY_POOL_MESSAGE`: they come from it, not from `dream_parser`. The cause is known and
legitimate — since 041's maturity filter, the candidate pool is regularly empty, and
`e8b59c3c` made that emptiness observable instead of silent. **`dream_parser` has not
written a single `promote` row since 2026-08-06.** §14.2's claim — "the only one writing
all six phases per project" — is therefore false for the `promote` phase, and has been
false since before §14.2 was written.

**Consequence for semantics, and this is what makes this batch non-negotiable.** Shipping
the table's five writers would leave `promote` at `NULL` every other night, while the other seven phases would carry a key. But `NULL` has a fixed meaning: "row written before
042". A post-042 `NULL` would destroy that — and it would be invisible, because the row
exists and reads `done`.

`promote` is a **per-project** phase. The sixth writer therefore gets the **real key**,
not the sentinel. It needs a required `--project-key` on its `record-empty-pool`
subcommand and its wiring from `scripts/dream.sh:490`.

### 15.2 The spec cites the right numbers for the wrong rail

§14.2 and §14.3 rely on `dream.sh:334` (invocation) and `dream.sh:338` (the
`WARN … non-fatal` that swallows the failure). **Both lines are the Claude branch, which
is the operator's explicit fallback and does not run in production.** `scripts/dream.sh:19`:

```bash
BRAIN_DREAM_AGENT_PROVIDER="${BRAIN_DREAM_AGENT_PROVIDER:-codex}"
```

The live rail is `dream.sh:326-331`, which invokes `brain_v42.metrics.codex_dream_parser` —
confirmed by `dream_runs`'s recent rows, all at `model='gpt-5.6-sol'`. §14.3's reasoning
("it raises, but its orchestrator catches it") stays **accurate**, because both branches
swallow the same way; only the line numbers point at the dead path.

**One `INSERT`, two entry points.** `codex_dream_parser.py:12` imports
`_insert_dream_run` from `dream_parser`, but has its **own** `_build_arg_parser`
(`:117-129`) and its own `main()`. Both consume the **same** `parser_args` array built
once at `dream.sh:315-317`. This produces a constraint the inventory couldn't express
while it was counting files:

- adding `--project-key` to `dream_parser` alone **fixes the dead rail and breaks the live
  one**: `codex_dream_parser` receives an unknown argument, `argparse` exits `2`, `set -euo
  pipefail` propagates, and `dream.sh:331` logs `WARN codex_dream_parser failed for $name
  (non-fatal)`. **The night loses its six per-project rows, silently, and the test suite stays
  green** — measured, `codex_dream_parser._build_arg_parser` and `main()` have **no test at all**.
- adding the required parameter to `_insert_dream_run` without touching
  `codex_dream_parser.py:159` produces the exact same failure, one layer down, as a `TypeError`.

This is the "a required parameter breaks the caller you didn't touch" trap, and it lands
on the one path that actually runs.

### 15.3 The corrected split: six writers, two keys, three swallows

| # | site | key | who swallows the failure |
|---|---|---|---|
| 1 | `metrics/dream_parser.py:181` (`_insert_dream_run`) | **real key** | nothing in Python; `dream.sh:331`/`:338` depending on the rail |
| 2 | `scripts/dream/_promote_helpers.py:125` | **real key** | caught in `main()`, `rc=1`; `dream.sh:499` downgrades it to `WARN` |
| 3 | `scripts/ticket_extract.py:752` | `'*'` | its own function — "Best-effort — never raises" |
| 4 | `scripts/roadmap_curate.py:1146` | `'*'` | same |
| 5 | `maintenance/session_sweep.py:94` | `'*'` | same — "the trace must never kill the phase" |
| 6 | `scripts/dream/cross_project_resonance.py:163` | `'*'` | nothing in Python; dead, no caller |

**§14.3 holds and strengthens.** It said "the five lose their trace silently"; there are
six, and **none surfaces its failure**. A `NOT NULL` would therefore turn a schema
error into a warning printed everywhere. The column stays nullable, and that's still not
caution.

> **Correction of 2026-08-09, after adversarial review.** This paragraph's first draft
> said "three swallowed in their function, **three** swallowed by the orchestrator."
> That's wrong, and wrong in a way the following paragraph already contradicted: the
> split is **3 + 2 + 1**. Three swallow in their own function; **two** are swallowed
> by the orchestrator — the **shared** site of the two parsers (`WARN … non-fatal`) and
> `_promote_helpers` (`WARN promote — empty-pool dream_runs row NOT recorded`); the sixth,
> `cross_project_resonance`, is dead, hence never run, hence never swallowed either.
> Counting the two parsers as two swallowing sites meant recounting them as two writers
> after §15.2 had established they're only one. **Second time in this spec that an
> elegance rests on a headcount** — §14.3's lesson applied to §15.3 and I didn't see it.

Corollary to state plainly: **four writers will set `'*'`, not three.** The versioned
docstring of `042_dream_runs_project_key.py` says "three", and both `CLAUDE.md` and
`docs/SCHEMA.md:1032` still carry "three of the five writers". No test pins these
sentences — their falseness would therefore be silent and durable, inside the very files
read to understand the column's semantics. This batch corrects them.

### 15.4 What dictates the SHAPE of the code, and isn't a preference

Five measured guards, none of which appears in §14's inventory. They leave only one
viable shape, and a naively written batch breaks them mechanically.

**(a) The sentinel never goes through the central validator.**
`canonicalize_project_key('*')` raises: `_KEBAB = ^[a-z0-9]+([:-][a-z0-9]+)*$`
(`src/brain_v42/models/project_key.py:23`). The "validate the key before writing"
reflex, legitimate everywhere else in the repo, would make all four sentinels raise —
and on three of them the exception is swallowed by design, so **the column would stay
`NULL` silently, every night, on the global phases**.

> **Corrected while writing the code.** This clause said "`'*'` is written as a **SQL
> literal**, in the query." That was over-specified: what matters is that the value be
> **unvalidated**, **outside the command line** (b) and **outside the signature** (c).
> A literal gives all three, but forces copying `'*'` into four independent SQL
> strings — and a typo in one of the four would be silent, since these writers swallow
> their failure. The shipped shape is a **named bound parameter** whose value comes
> from **one shared constant**, `brain_v42.dream_run_project_key.GLOBAL_PHASE_PROJECT_KEY`. It
> satisfies all three requirements and removes the class of typo the literal reopened. A test
> pins that all four import that same constant, not a copy.

**(b) The global phases gain no CLI flag.** Two pins forbid it:
`tests/unit/test_dream_sh_sweep.py:57` asserts `appended == ["--wet"]` on the arguments
added to the SWEEP block, with a comment explicitly stating one more flag **must** fail
the test; `tests/unit/test_dream_sh_extract.py:33` pins an exact literal. These two
guards aren't obstacles to work around: they prove the sentinel belongs to the phase's
Python code, not to the command line. The shape they impose is exactly the one §14.3
had inferred for other reasons.

**(c) `record_dream_run`'s signatures don't change.**
`tests/unit/maintenance/test_session_sweep.py:100` calls the function **positionally**,
and about twenty test sites patch these writers as `AsyncMock()` without ever asserting
arguments. Adding a parameter — even with a default — breaks a real call and wakes none of the
blind tests: the worst ratio. Since the sentinel enters as a SQL literal, the signature has no
reason to change. **The corollary is that these four sites have, today, no test witness
reading the emitted SQL.** The batch must create one per writer — that's where the red lives.

**(d) `project_key` slots in at `$14`, `phase_dry_run` stays last.**
`tests/unit/metrics/test_dream_parser_phase_dry_run.py:98-101` reads the compiled SQL and pins
`"$14" in sql` **and** `bind_args[-1] is True`. Placing `project_key` at the end of `VALUES`
would turn this pin red for a reason unrelated to what it guards — and the immediate temptation
would be to "fix the test", which the project's TDD forbids. Inserting it **before**
`phase_dry_run`, the pin's intent ("`phase_dry_run` is bound last, and it's a real boolean")
survives intact and can be reinforced with an assertion on `$15`. This test's docstring, which
says "`$14` = `phase_dry_run`", becomes wrong and gets fixed in the same gesture.

**(e) No shared constant under `scripts/`.** The measured layering allows `scripts → src`
(practiced about twenty times) and **forbids** `src → scripts` — not by the guard, which only
sees `brain_v42` (`scripts/check_module_layering.py:43`), but by the `Dockerfile`, which never
copies `scripts/`. A `src → scripts` import would be **green locally, green in CI, and would
break the production image at import time**. And a new module at the package root must import
**nothing** from a sub-package: the graph measures `_root: []` while eight sub-packages target
`_root` — one outgoing edge closes eight cycles and makes the guard exit `rc=2`, **before**
pytest. A sentinel constant is therefore either a repeated literal, or a root module with no
import at all.

### 15.5 The flaw living one layer up — decided on 2026-08-09

The decision "`--project-key` required with no default" only covered, as written, the
Python binary. `scripts/dream.sh:70` already carries:

```bash
PROJECT_KEY="${1:-brain-v42}"
```

A bare `bash scripts/dream.sh` therefore satisfies the required flag with `brain-v42` and
labels the whole night under another project — **exactly the class of bug the decision
targets**, one layer above where the guard was placed. The precedent already exists in
production: `scripts/dream/post_run_alert.py:157` carries `default=DEFAULT_PROJECT_KEY`, while
`scripts/dream/promote_prepare.py:131` carries `required=True`. The repo holds both forms; a
choice must be made about which one is the reference.

**Decided by the operator: the default is removed, in a separate commit.** `dream.sh` will
require its positional argument. No night changes behavior — systemd already passes `brain-v42`
explicitly (`ExecStart=… scripts/dream.sh brain-v42`) and the six test harnesses already pass
`test-project`. The commit is separate from the writers' commit, so it's revertible on its own.

### 15.6 What this batch does not do, and a reader inventory to redo

- **It doesn't touch the readers**, and §14.6 stays true in substance: none changes its
  result tonight. But its reasoning is wrong. One reader has **already** changed its SQL at
  batch 2's merge: `sa.select(t)` in `DreamRunService.last_failure` emits
  `dream_runs.project_key` ever since `tables.py:1253` declared the column. The data is dead
  because no one reads it **inside a predicate**, not because no one selects it.
- **§14.2's reader inventory is undercounted on two axes**, and the readers batch will have
  to start over from a measurement, not from this list. Three whole files are missing:
  `src/brain_v42/services/dream_run_service.py` (**six** `SELECT`s, and it's
  `brain_session_start`'s briefing), `src/brain_v42/services/brain_graph_projection.py:232`
  (the column allowlist projected into Neo4j nodes), and migration 036's persistent view
  `codex_dream_run_v1`. Measured: **at least eighteen `SELECT`s across ten sites**, versus
  "ten across seven".
- **It doesn't touch any of §14.7/§14.8's seven guards.** They're already at `042` and
  green; "fixing" them would be a false positive on a cutover already consumed. §14.7
  describes the state *before* the bump and is stale as a guideline.
- **It doesn't wire up `cross_project_resonance`.** It gives it `'*'` so it isn't the only
  inconsistent one the day someone reconnects it. Wiring it up or removing it stay two
  separate decisions, not made.
- **It opens no loop.** §2.3 forbids deriving scheduling from this column, and §15.3's
  table reinforces the ban: **six** writers can write nothing without anyone knowing.

### 15.7 What this pass could not do

- **No night has been observed with the column filled in.** Everything above is reading
  code, measuring the database on rows written *before* the batch, and running the test
  suite. Proof that a writer actually sets its key will come from the night after the
  merge, not from here.
- **The row count shifts every night**: §14.1 says 864, the 2026-08-09 measurement says
  872. No test in this batch should pin a row count — it would go red the next
  morning, at 06:00, with no one having changed anything.
