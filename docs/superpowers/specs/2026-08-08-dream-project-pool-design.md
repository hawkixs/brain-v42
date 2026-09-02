# Nightly dream project pool — breaking out of mono-project

**Date**: 2026-08-08
**Status**: design; six questions settled, two points left open and named;
**passed adversarial review on 2026-08-08 — figures replayed, corrections in §19**
**Scope**: `brain_v42` only — `scripts/dream.sh`, the phases, `dream_runs` and its readers
**Worktree**: `.claude/worktrees/dream-pool`, branch `feat/dream-project-pool`, base `b55b7590`
**Migration measured in production at time of writing**: `041`
(`docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"`)
**No implementation code accompanies this spec.** `scripts/dream.sh` runs
from the **root** working tree at 06:00; a half-written loop would run
that night on eight projects.

---

## 1. What is decided upstream

The operator has fixed the pool. This is not an open question in this spec:

```
brain-v42   red   auto-discord   red-lab   red-shrik   red-writer   red-watcher   red-monitor
```

Eight **parent keys**, exactly. Measured mass (`learnings ∪ decisions ∪ snippets ∪
runbooks ∪ adrs`, `GROUP BY project_key`):

| key | artifacts |
|---|---|
| red | 859 |
| brain-v42 | 658 |
| red-writer | 252 |
| red-shrik | 222 |
| red-lab | 161 |
| auto-discord | 113 |
| red-monitor | 77 |
| red-watcher | 31 |
| **pool** | **2 373** |

The full corpus is **3 803** artifacts, spread across 54 `project_contexts`. **26 of these
artifacts** carry `project_key IS NULL`; no `project_context` has a null key (measured:
`SELECT count(*) FROM project_contexts WHERE project_key IS NULL` → 0). The pool covers
**62,4 %** of the corpus (2 373 / 3 803).

`red` is bigger than `brain-v42`. This is the fact that motivated the pool, and §4 shows
why it changes **nothing** about the cost of a night as long as the server scope stays off.

---

## 2. The exclusion of sub-projects, and its cost

The six two-level keys are **deliberately outside the first batch**. Measured cost:

| excluded key | artifacts |
|---|---|
| red-shrik:agent | **280** |
| red-lab:architect | 113 |
| red-lab:orchestrator | 64 |
| red-lab:reviewer | 15 |
| red-lab:sentinel | 5 |
| red-lab:developer | 2 |
| **total excluded** | **479** |

**`red-shrik:agent` (280) is bigger than `red-shrik` itself (222).** The excluded
sub-project weighs more than the included parent. This is the clearest form of the debt: the pool
will not consolidate the heavy half of `red-shrik`.

479 artifacts = **20,2 % of the pool's mass**, outside consolidation for an undecided
duration.

There is **no** prefix filter in the pipeline at all. `scripts/dream/promote_prepare.py`
filters by exact equality:

```sql
AND l.project_key = :pk
```

So `red-lab` will never see `red-lab:architect`, and no closing of this debt will
happen "by itself": either the six keys will have to be added to the pool — six more runs,
+6 × 53 min of configured ceiling (§10) — or a prefix semantics will have to be introduced that
exists nowhere in the code today. Both are projects, not
settings.

**Debt owned, not forgotten.** It must be carried by a ticket at merge time, not
by this sentence.

---

## 3. What breaks before the six questions even start

Five couplings are not trade-offs: they make the loop **wrong** if they are not
handled. They precede any discussion.

**3.1 — The lock is global.** `scripts/dream.sh:351`:

```bash
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/brain-v42-dream.lock"
```

`flock -n 9` with no project component. The "eight systemd units" topology is therefore
**structurally impossible**: the following seven invocations would exit with `exit 0`
with "dream cycle already running, skipping". A green night where seven projects out of eight
did nothing. This is the worst failure mode in the batch — silent and green. **The pool is
a loop inside one run, not eight runs.**

**3.2 — All log paths are dated, none are projected.**
`scripts/dream.sh:74`: `TIMESTAMP=$(date +%Y-%m-%d)`. Twelve path templates depend
on it, and `scripts/dream/codex_runner.py:419` does `report_log.write_text("")` while
`:432-433` open `events` and `stderr` in `"w"` — **truncation**, not append. In the
morning, only the last project's logs survive.

**But the projection does not apply to all twelve.** The sorting is decided by §7 and §11, not
by this paragraph. Of the twelve templates found (`grep -n 'LOG_DIR/' scripts/dream.sh`):

| template | projected? | why |
|---|---|---|
| `${TIMESTAMP}_${name}.log` (report), `.raw.log`, `.otel.log`, `.events.jsonl`, `.stderr.log`, `.err.log` | **yes** | truncated by `codex_runner`, and the report feeds `PHASE_DEPS` (§3.3) |
| `${TIMESTAMP}_promote_candidates.json` | **yes** | this is not a log but a **validator input** (`promote_validate --candidates-json`); without projection, no post-mortem recovery per project is possible |
| `${TIMESTAMP}_promote.log` (summary report "empty pool", `:481`) | **yes** | same template as the phase report, written with `>` |
| `${TIMESTAMP}_extract.log`, `_roadmap.log`, `_sweep.log` | **no** | global phases, a single execution per night (§7) — projecting them would manufacture seven empty files |
| `$TIMESTAMP.log` (main log) | **no** | opened with `tee -a` / `>>`, it is the single narrative of the night and the redirection target of the single aggregated alert (§11). Fragmenting it into eight would contradict Q6. |

Seven templates gain a project component, five do not. A uniform
instruction of "every path" would produce code that is wrong in both directions.

**3.3 — Loop order is a correctness decision.** `scripts/dream.sh:205-226`
injects the previous phase's report **by re-reading a file**:
`dep_log="$LOG_DIR/${TIMESTAMP}_${dep}.log"`, `PHASE_DEPS` table at `:116-123`. In
phase-major order (all the `scan`s, then all the `clean`s…), `red`'s CONNECT would receive
`red-writer`'s CLEAN report. **The loop must be project-major**: one project, its six
phases, then the next.

**3.4 — Two exported variables survive iterations.** `scripts/dream.sh:504-510`
exports `PROMOTE_CANDIDATE_POOL_JSON` and `PROMOTE_RECENT_PROMOTIONS_JSON`, re-read without
reinitialization at `:196-197`. If project N skips PROMOTE (killswitch, empty pool, failure
of `promote_prepare`), project N−1's value stays loaded. **Reset to `[]` at the top of
each project iteration**, otherwise one project promotes on another's pool.

**3.5 — Two prompts hardcode `brain-v42`, and it is the only irreversible defect.**

```
scripts/dream/phase_synth.md:24   project_key="brain-v42",
scripts/dream/phase_synth.md:58   - Always use `project_key="brain-v42"` … for insights.
scripts/dream/phase_promote.md:4  Project scope: {{PROJECT_KEY}} (v1 is brain-v42 only — …)
scripts/dream/phase_connect.md:43 memory — knowledge graph, brain-v42, embeddings, …
```

SYNTH run for `red` would create insights and snippets labeled `brain-v42`. At
the prompt's ceilings (`phase_synth.md:55-56`) and across eight projects: **up to 24 insights and
24 snippets per night, misattributed**. No migration fixes this — it is content
written into the wrong project. **These four lines land before the loop, not with it.**

And `scripts/dream/promote_validate.py:67-179` does **not** check `project_key`: it
checks `target_type`, the candidate's identity, `adrs.status == 'accepted'` and the line
count. `phase_promote.md:122`'s promise ("`project_key` … MUST equal
`{{PROJECT_KEY}}`") is prose alone. Across eight projects, an ADR materialized on the
wrong project passes the gate.

---

## 4. The wall-clock — method first, figures after

### 4.1 The assumption that drives everything, and that is measured

In **five** of the six phase prompts, `{{PROJECT_KEY}}` only appears in the `## Mode`
header, in prose. No tool call receives it, and the server signatures do not
expose it: `brain_decay_status()` (`decay_tools.py:56`) has no parameter,
neither do `brain_consolidation_candidates`, `brain_backfill_links_batch`,
`brain_list_orphans_for_classification` or `brain_get_clusters`.

**Consequence: SCAN, CLEAN, CONNECT, SYNTH and REORG run eight times do eight times the
same work on the same global corpus.** A run's cost is driven by constant call
ceilings, not by the project's size. That `red` (859) is bigger than
`brain-v42` (658) changes nothing.

This is what authorizes the method below. **This assumption falls the day the server
scope is armed** (§14) — everything will then have to be re-measured.

### 4.2 Bound (a) — simulation over real nights, not over averages

Method: break down each of the last 30 nights into an "agent phases" subtotal
(scan, clean, connect, synth, promote, reorg) and a "global phases" subtotal
(extract, roadmap, sweep), then evaluate `8 × agent + global` **night by night**, and finally
take the mean, the p90 and the max of that series. Multiplying averages would have lost
the distribution's tail, which is precisely what threatens the 06:00 window.

Query actually run:

```sql
WITH n AS (
  SELECT run_date,
         sum(duration_s) FILTER (WHERE phase IN ('scan','clean','connect','synth','promote','reorg')) AS agent_s,
         sum(duration_s) FILTER (WHERE phase IN ('extract','roadmap','sweep'))                        AS global_s
  FROM dream_runs
  WHERE run_date >= (SELECT max(run_date) FROM dream_runs) - 29
  GROUP BY run_date)
SELECT ... avg / percentile_cont(0.9) / max ... FROM n;
```

| topology | mean | p90 | max |
|---|---|---|---|
| today (1 project) | **15,05 min** | 21,22 | **72,76 min** |
| 8 × agent + 1 × global | **80,0 min** | **126,6 min** | **286,3 min** (4,8 h) |
| 8 × everything (naive) | 120,4 min | — | **582,1 min** (9,7 h) |

Breakdown by phase, seconds consumed per night over these 30 nights (re-runs included):

| phase | s/night | nature |
|---|---|---|
| roadmap | 259,9 | **global** |
| synth | 210,3 | in the loop |
| reorg | 187,6 | in the loop |
| extract | 86,1 | **global** |
| clean | 62,6 | in the loop |
| scan | 42,8 | in the loop |
| connect | 28,0 | in the loop |
| promote | 25,8 | in the loop |
| | agent subtotal **557,1 s** · global **346,0 s** | total 903,1 s = 15,05 min |

**What bound (a) assumes, and does not measure.** The `×8` applies to **runs**, not
to artifacts: no linear extrapolation on a project's mass is made or
justifiable here, and §4.1 is what forbids it. What remains is a bias whose **sign is unknown**:
with the scope off, runs 2 through 8 work on a corpus that run 1 has just mutated.
SYNTH inflates it (which pushes upward), while CLEAN, CONNECT and REORG exhaust
their own work queue on the first pass (which pushes downward — an agent that
finds nothing left to do returns control faster). Neither is measured: no
two-project night has ever run. Secondary bias, measured and negligible: the pre-flight
has cut the deep phases 2 nights out of 121 (one of them within the 30-night window), and it will no
longer do so once eight SYNTHs are running (§16).

### 4.3 Bound (b) — the configured ceiling, which is what systemd kills

`PHASES` (`scripts/dream.sh:107-114`): `scan:5 clean:5 connect:8 synth:15 promote:10
reorg:10` = **53 min**. The retry (`:539`) is unique, only on hard failure, and excludes
`promote`: **+43 min** eligible → **96 min per project**.
Globals: `timeout 10m` extract (`:664`), `timeout 20m` roadmap (`:706`), `timeout 5m`
sweep (`:735`) = **35 min**.

| scenario | ceiling | vs `TimeoutStartSec=10800` (180 min) |
|---|---|---|
| today | 131 min | 49 min of margin |
| **2 projects** | **227 min** | **already exceeded** |
| 8 projects, no retry | 459 min (7,7 h) | ×2,6 |
| 8 projects, with retry | 803 min (13,4 h) | ×4,5 |

**The current systemd ceiling does not hold for two projects.** The unit is `Type=oneshot`, the timer
`OnCalendar=*-*-* 06:00:00` with `Persistent=true` (read from the live files). A
trigger whose unit is still active does not start a second instance: it is
lost. Honest correction, against the earlier version of this paragraph: **this scenario is
not reachable with these figures** — the worst configured case is 803 min (13,4 h), and
`TimeoutStartSec` kills it well before that anyway. The real risk is not overflowing
24 h, it is **systemd killing the night in the middle of a project**, leaving the following projects
with no line at all in `dream_runs` — invisible to a `DISTINCT ON (phase)` reader who
will see the phases of the projects already processed.

### 4.4 Tokens and cost

Measured over the same 30 nights, with a trap that must be named: `cache_creation_tokens`
is `NULL` on **167 lines out of 272**. The naive sum
`sum(input+output+cache_read+cache_creation)` propagates the `NULL` and **loses 61% of the
lines** — it gives 190 239 tokens/night. With `COALESCE(...,0)`:

| measurement | value |
|---|---|
| tokens/night, mean | **1 455 688** |
| tokens/night, max | **3 850 807** |
| `cost_usd`/night, mean | **0,6425 $** |
| `cost_usd`/night, max | 4,6846 $ |

At eight projects: ~11.6 M tokens/night on average, ~30.8 M on the worst night measured. The
dollar cost has been zero since the switch to subscription authentication — **that
subscription's rate ceiling is the real risk, and it is not measurable from
here** (§17).

---

## 5. Q1 — Does a 042 migration have to happen?

### Decision: yes. `dream_runs.project_key VARCHAR(64) NULL`, no backfill, with an index `(run_date DESC, project_key)`. The global phases write the sentinel `'*'` into it.

**There is no alternative without DDL.** Measured:

```
phase | character varying(10) | not null
```

A composite value of the form `synth@red-writer` is 16 characters and **does not fit**.
And there is no usable index for a per-project filter: `dream_runs_pkey(id)` and
`idx_dream_runs_date(run_date DESC)`, nothing else.

**Without the column, ten readers lie, and three of them write.** This is not a
display problem:

| reader | file | what happens at eight |
|---|---|---|
| `_dream_run_id` | `scripts/dream/_promote_helpers.py:86-101` | `ORDER BY id DESC LIMIT 1` → returns the **last project**; this `dream_run_id` then serves to mark `partial` (`promote_validate.py:181-196`) and to **backfill `dream_promotions.dream_run_id`** (`:122-127`) |
| `_mark_latest_connect_partial` | `scripts/dream/connect_validate.py:91-115` | same on CONNECT |
| `REORG_RUN_ID` | `scripts/dream.sh:601-622` | same on REORG |
| `/dream` payload | `metrics/collector_dream.py:81-95` | `DISTINCT ON (phase)` → **one** line per phase: a failed `synth` on `red` is **erased** by a successful `synth` on `red-writer` |
| 10-day history | `metrics/collector_dream.py:155-182` | same erasure; the `SUM(cost_usd)` values stay correct, the `day_status` lies |
| `last_failure` | `services/dream_run_service.py:218-237` | the `brain_session_start(project_key="brain-v42")` briefing will display `red-watcher`'s failure |
| `killswitch_state` / `_clean_dry_streak` | `services/dream_run_service.py:76-188` | see below |
| `fetch_failed_runs` | `scripts/dream/post_run_alert.py:90-124` | filters on `run_date` alone (see Q6) |
| `last_failure` sidecar | `metrics/collector_nightly.py:78-86` | second copy of the same defect |
| `expected_dream_phases` | `metrics/collector_dream.py:27-48` | see Q2 |

The first three **write a false and durable attribution** into the promotions
audit trail. This is what makes the delivery order non-negotiable (§12).

### Nullable, never `NOT NULL`

Five INSERT sites write into `dream_runs`, and **three of them do it in raw SQL swallowed
best-effort**:

```
src/brain_v42/metrics/dream_parser.py:181       INSERT INTO dream_runs (…14 colonnes…)   ← asyncpg brut, partagé avec codex_dream_parser.py:159
scripts/ticket_extract.py:752                   INSERT INTO dream_runs …   except Exception: print(f"! warning: …")
scripts/roadmap_curate.py:1146                  INSERT INTO dream_runs …
src/brain_v42/maintenance/session_sweep.py:94   INSERT INTO dream_runs …
scripts/dream/_promote_helpers.py:125           sa.insert(dream_runs).values(…)
```

(a sixth, `scripts/dream/cross_project_resonance.py:163`, is not wired anywhere.)

A `NOT NULL` column with no default would produce **invisible telemetry loss**
there: the INSERT fails, the exception is printed as a warning, the night stays green and the
line does not exist. The column is nullable, and it is the code that fills it in.

### The backfill, confronted with the repository's doctrine

The doctrine is explicit: *no backfill, `NULL` means "never measured"* (040 for
`focus_updated_at`, 041 for its three columns). It has to be confronted, not applied
by reflex.

**The argument FOR backfilling to `'brain-v42'` is solid.** The 856 lines were
produced by a process whose project argument is hardcoded in three places:
`ExecStart=… dream.sh brain-v42` in the **live** unit (`~/.config/systemd/user/
brain-v42-dream.service:28`, read in production), in the versioned template
(`deploy/systemd/brain-v42-dream.service.tmpl:31`), and guarded by an integration test
(`tests/integration/test_dream_systemd_install.sh:180`). This is not an inference about an
unobserved event — it is a configuration constant.

**The argument AGAINST holds anyway, and it wins.** Two reasons:

1. `dream.sh` accepts a positional argument (`:70`). A manual invocation with a
   different key is possible and **indistinguishable after the fact**. The backfill would assert 856
   times something that cannot be verified line by line. The doctrine exists
   exactly for this case.
2. **The cost of `NULL` has been measured, and it is zero.** The serious objection was:
   "`_clean_dry_streak` counts clean DRY nights, and that is the operational argument
   for flipping a killswitch to WET; filtering by project would reset all the counters to
   zero". Measurement of the five streaks:

   | phase | `reset_date` | clean DRY nights |
   |---|---|---|
   | roadmap | 2026-07-14 | **24** |
   | promote | 2026-08-06 | 0 |
   | reorg | 2026-08-07 | 0 |
   | extract | 2026-08-07 | 0 |
   | sweep | — (no line) | — |

   **The only counter worth anything today belongs to `roadmap`** — a
   global phase, which stays outside the loop (Q3) and therefore keeps a single writer per
   night. `promote` and `reorg`, the only two streaked loop phases, are already at
   0. `NULL` does not destroy any useful history.

`NULL` = "before the pool". Readers must display it as a third state, not
make it disappear via a `WHERE project_key = :pk`.

### The `'*'` sentinel for the global phases

For extract, roadmap and sweep, the project is not "unmeasured": it is **not
applicable**. These are two different things, and conflating them would revive the same
ambiguity as the backfill. These three phases write `'*'`. `dream_runs` has no foreign
key to `project_contexts`, so nothing forbids an out-of-domain value; it is a
telemetry column, not a reference.

### What we lose

- The 856 historical lines drop out of every per-project view. "Before/after
  pool" comparisons will need an explicit `IS NULL`, never a `= 'brain-v42'`.
- One more index on a table that takes ~7 lines/night today and will take 51 —
  negligible cost, but it is one more migration in a chain that already numbers 41.
- The `'*'` sentinel is a value that no project-key validation would accept.
  Any code that reused this column as a valid project key would be wrong.
  To write in the column comment, not just here.

---

## 6. Q2 — Should killswitches be gated per project?

### Decision: no. The six killswitches stay global. Per-project gating is **pool membership**, expressed by a single variable in the same drop-in.

```
Environment=BRAIN_DREAM_PROJECT_POOL=brain-v42
```

Default in `dream.sh` (idiom identical to `BRAIN_DREAM_SWEEP_*` at `:58-59`): `brain-v42`
alone.

### Two transport traps, to handle together with the variable itself

**"Adding a word to a line" does not work.** systemd's `Environment=` splits on
unprotected whitespace and treats each chunk as a separate assignment:
`Environment=BRAIN_DREAM_PROJECT_POOL=brain-v42 red` sets the variable to `brain-v42` and drops
`red` (chunk with no `=`). **The pool would shrink to one project, silently, with no error at
startup** — exactly the green failure mode that §3.1 rules out. Two ways out, to be decided at
implementation time, never left implicit:

- **comma** separator (`…=brain-v42,red,red-lab`) — no protection needed;
- or **explicitly protected** whitespace (`Environment="BRAIN_DREAM_PROJECT_POOL=brain-v42 red"`).

A test must pin the chosen format against the real drop-in, otherwise the first opening of the
pool will be a one-project green night.

**The shared parser cannot read this variable.** `parse_killswitches`
(`src/brain_v42/dream_killswitches.py:26-37`) returns a `dict[str, bool]`: it ignores any key
absent from `_KS_KEYS` and coerces the value via `value.strip('"').lower() == "true"`. A key with a
list value does not fit there. But §6 requires below that `expected_dream_phases` read the pool —
so this batch **touches the shared parser regardless** (a new key in `_KS_KEYS`, or more
cleanly, a second function that returns the raw values). Argument 2 below charges this
cost to the rejected matrix; it must be acknowledged that it is not zero on the chosen side either.
It remains much smaller: **one** list key against **48** boolean lines.

Three measured reasons:

1. **Three of the six killswitches have nothing to gate per project.** EXTRACT, ROADMAP and SWEEP
   drive **global** phases that sit outside the loop (Q3): "arm EXTRACT for
   `red` only" makes no sense, the ticket queue is global
   (`scripts/ticket_extract.py:471-486`, no project filter). That leaves only
   PROMOTE and REORG, i.e. **two** families, not six.
2. **8 × 6 = 48 `Environment=` lines would have to be maintained by hand** in a file that
   already carries four paragraphs of incident comments. The
   `Environment=KEY=value` format has no project dimension, and `_KS_KEYS`
   (`src/brain_v42/dream_killswitches.py:12-22`) is a flat dictionary shared by
   **three** application readers: `dream_run_service._read_killswitch_flags` (session
   briefing), `collector_nightly.collect_nightly_ops` (`/metrics` payload) and
   `collector_dream.expected_dream_phases`. A matrix would force touching all three, or
   the briefing and `/metrics` would lie about the armed state.
3. The live drop-in, read today, shows the real regime: `PROMOTE=true`, `REORG=true`,
   `REORG_DRY_RUN=false`, `EXTRACT=true`, `EXTRACT_DRY_RUN=false`, `ROADMAP=true`,
   `ROADMAP_DRY_RUN=true`, `SWEEP_*` **absent** (hence `false`/`true` by default). These are
   cadence decisions, taken phase by phase after soak — not project
   decisions.

### The non-negotiable constraint that comes with it

`expected_dream_phases()` (`metrics/collector_dream.py:27-48`) turns "phase armed"
into "alarm if absent from `dream_runs`". This is the anti-silent-crash mechanism from
2026-05-02, and it is consumed by `post_run_alert.include_missing_expected_phases:69-87`
and `collector_dream:138-150`.

**At eight projects, it disarms itself**: if a single project skips `promote`, the phase
stays "observed" globally thanks to the other seven, and the alarm stops sounding.

So the expected set must become a **Cartesian product** `{phase} × {pool project}` for
loop phases, and stay a singleton for the globals. This transformation reads the
new pool variable. **It lands with the loop, never after.**

### What we lose

- No way to say "REORG in WET on `brain-v42` but in DRY on `red`". The only lever
  is binary: the project is in the pool, or it isn't. A project one would want to
  scan without reorganizing has no way to express that.
- The first soak of a new project therefore happens **under the WET regime already in force** for
  REORG and EXTRACT. This is the real reason the pool is opened one key
  at a time (§12), not in one block.

---

## 7. Q3 — Where does `sweep` come out of the loop?

### Decision: **all three** global phases come out of the loop, not just `sweep`. They run once, after the project loop, and write `project_key='*'`.

The question as asked only named `sweep`. The measurement says there are three, and
the other two cost more.

**`sweep` — global by construction.** `src/brain_v42/maintenance/session_sweep.py:39-57`:
the parser exposes `--wet` and `--older-than-days`, **nothing else**.
`PgBrainSessionRepo.abandon_stale` filters on `status == 'open'` and the heartbeat, with no
project. At eight runs: the first abandons, the following seven find nothing and
write seven extra `done` lines — which would inflate `_clean_dry_streak` by seven
fictional "nights" per real night.

**`extract` — global by construction.** `scripts/ticket_extract.py:471-486`:
`WHERE extraction_status = 'pending' ORDER BY closed_at ASC LIMIT :limit`, no project
filter. The first run empties the queue up to `--limit 20`, the following seven run
empty **while still consuming their `--run-budget-seconds 540`**.

**`roadmap` — already multi-project, with its own rotation.**
`scripts/roadmap_curate.py:437-441` selects `DISTINCT project_key FROM features
WHERE status NOT IN ('done','archived') AND merged_into IS NULL`, and
`rotate_keys(keys, limit, day_ordinal)` (`:474-487`) does a sliding window of `--limit`
**projects** per night. `--limit 10` (`dream.sh:698`) therefore means **10 projects**, not 10
features.

The rotation domain is **not** the 54 `project_contexts`: it is this query, measured
at **30 projects** at time of writing (of which six two-level keys and several projects
outside the pool). Full coverage therefore happens in ⌈30/10⌉ = **3 nights**, not 6. This domain
is dynamic — it shrinks as features move to `done`/`archived` — so it gets
re-measured, it is not copied forward. Since
`day_ordinal = date.today().toordinal()` is identical across the eight invocations, the **same
window would be curated eight times**, at eight times the API budget. This is the most
expensive phase of the night (259,9 s/night measured) and the one that needs the loop the least.

**Structural guarantee, not convention.** The three calls must be **outside the body**
of the project loop, and a test must pin this textually — the idiom already exists
(`tests/unit/test_dream_sh_agent_provider.py:74` asserts `'--project-key "$PROJECT_KEY"' in
content`). A convention gets lost at the first refactor; a textual anchor fails
loudly.

### What we lose

- `sweep`'s scope stays **global**, hence different from the pool. It will keep
  abandoning sessions from projects outside the pool. This is intentional (§8), but it creates two
  sets that must never be conflated: "the pool" and "what `sweep` touches". To
  write into CLAUDE.md at rollout time.
- `roadmap` keeps its own rotation over **30** projects (measured), independent of the pool of
  8 and not containing it. Two notions of "projects processed tonight" coexist within the
  same run. No one should read the roadmap window as pool coverage.

---

## 8. `sweep` — the only irreversible phase

It deserves its own section, for two properties no other phase shares.

**It is the only irreversible phase.** It moves sessions to `abandoned`.
`brain_session_resume` requires `status='open'`: the abandonment is terminal, and there is
no `unabandon` (explicit decision of the 2026-08-07 spec: do not build a
reversal for a problem that has not been observed). The other five phases of the night propose, or
write artifacts that a REORG can pick back up. `sweep` closes.

**It is the only phase that would act on projects where the dream has never
consolidated anything.** Measurement of open sessions and 7-day ghosts:

| project_key | open | 7-day ghosts | in the pool? |
|---|---|---|---|
| red-lab | 2 | **2** | yes |
| red-watcher | 1 | **1** | yes |
| claude-dev-pc | 1 | **1** | **no** |
| red-codex | 1 | **1** | **no** |
| red-story | 1 | **1** | **no** |
| red-viewer | 1 | **1** | **no** |
| brain-v42 | 1 | 0 | yes |
| red-monitor | 1 | 0 | yes |
| red-arena, datalake-v1 | 1 + 1 | 0 | no |

**7 eligible ghosts: 3 in the pool, 4 outside the pool. Zero for `brain-v42`.**

This is §7's decisive argument taken in reverse: if `sweep` became per-project and looped
over the pool, **4 ghosts out of 7 (57%) would become unreachable**. The phase that needs
the pool the least is also the one the pool would harm the most.

Three consequences for the design:

1. `sweep` stays out of the loop, global scope, `project_key='*'` in `dream_runs`. No
   "pool" variant is proposed, not even as an option.
2. It stays **closed and dry** (`BRAIN_DREAM_SWEEP_*` absent from the drop-in, hence
   `false`/`true`). It has **never written a single line** into `dream_runs`: the table
   counts 856 lines across 8 phases, and `sweep` is not among them. Its DRY soak is still
   ahead of it, and the pool must absolutely not get ahead of it.
3. Opening `sweep` and opening the pool are **two independent decisions**, never
   to ship in the same batch. An irreversible phase does not get armed the same night
   as a topology change.

---

## 9. Q4 — Failure isolation: project 3 fails, do projects 4 through 8 run?

### Decision: yes, full isolation. No quorum, no threshold. The counters stop being flat arrays of **phase names** and become `project/phase` pairs. The exit gate keeps its three conditions, word for word.

The real gate code, `scripts/dream.sh:801-811`, reworked by `b3c3e0fd` and
`f47427d0`:

```bash
if (( ${#FAILED_PHASES[@]} > 0 )) \
  || (( ${#TIMED_OUT_PHASES[@]} > ${#CONTROLLED_TIMEOUT_PHASES[@]} )) \
  || (( alert_rc != 0 )); then
  exit 1
fi
```

Its comment says why it is **structural, not arithmetic**: the original form
subtracted `${#CONTROLLED_TIMEOUT_PHASES[@]}` from the total, and a phase mistakenly registered
in both `FAILED` **and** `CONTROLLED` erased its own failure — the script exited 0
after printing "1 failed (synth)".

**What eight projects do to it.** `TOTAL_PHASES` goes from 9 to **51** (8 × 6 + extract +
roadmap + sweep). The `:766-776` summary would print `3 failed (synth synth synth)`:
**three indistinguishable projects**. The arrays are unlabeled multisets of
names.

Fix: `FAILED_PHASES+=("$project/$name")`, same for `TIMED_OUT_PHASES`,
`CONTROLLED_TIMEOUT_PHASES` and `SKIPPED_PHASES`. The summary becomes readable again without changing
a single gate condition.

**Why no threshold.** A "7/8 projects OK = green" quorum would be exactly the defect
that `2026-08-07` fixed, turned around: the original gate went red every night and
had become mute; a threshold would make the gate green too often and mute it
at the other end. As long as no eight-project night has been observed, there is **no
measurement** to calibrate a threshold. We keep the fail-closed form.

**But the red-night rate itself can be measured — and it has to be reported here.** Frequency
of at least one non-`done` agent line appearing in `dream_runs`:

| window | red nights (agent side) | rate |
|---|---|---|
| 121 nights (entire history) | 20 | **16,5 %** |
| last 30 nights | 2 | **6,7 %** |

Under the assumption of independence between projects — an assumption that is false in the conservative
direction, a Codex outage hitting all eight at once — an eight-project night goes red with probability
`1 − (1 − p)⁸`, i.e. **42% at the recent rate and 77% at the historical rate**, against 6,7% and
16,5% today.

This figure does not overturn the decision: the fail-closed exit remains the right form, and
2026-08-07 showed that an arithmetic counter masks real failures. It changes what is
promised to operations. **A unit that is red one night out of two turns back into a state with no
information** — the same wall that `b3c3e0fd` tore down, reached from the other side. Two
consequences to own explicitly rather than discover:

1. The useful signal is no longer the unit's exit code but the **content** of the
   per-project aggregated report (§11). That is what must be made readable first, not the color.
2. The threshold is only reopened for discussion **after** measuring a series of nights with the pool open. This is
   the real reason to open one key at a time (§12 step 6): each addition gives a measurement
   point on this rate, before it is too late to back out.

**Cost accepted, and it is real**: a `synth` failure on `red-watcher` (31 artifacts)
reddens the unit exactly as much as seven failures on `red` (859). There is no
"mostly OK" state that can be expressed. If operations shows that this state is missing, it will have to
be measured first, not postulated.

**Shell mechanics not to miss.** `set -euo pipefail` is active (`:2`), and five `continue`
(`:447, 455, 469, 501, 522` — found via `grep -n '^\s*continue\s*$'`) belong to the
**phase** loop. An enclosing project
loop would turn them into `continue` of the wrong loop: the project would move to the
next **project** iteration instead of the next phase. Five sites to requalify,
and this is the kind of bug that turns a night green while it did nothing.

Existing safety net, measured: `tests/unit/test_dream_sh_exit_code.py` (21 tests) **slices and
runs real shell blocks** via textual anchors (`:35-39`: `FAIL_TOTAL=$((`,
`if (( extract_rc == 0 )); then`, `# --- ROADMAP`, `case "$phase_rc" in`, `esac`). These are
`content.index()` calls: a reshuffle that moves these blocks fails **loudly**
(`ValueError`), not silently. That is this question's safety net.

---

## 10. Q5 — Budgets: per project or shared across the night?

### Decision: **per project for the six agent phases** (they have no choice: the budget is a `timeout` per invocation), **single and unchanged for the three global phases** (they only run once), and **a retry ceiling for the entire night** — because the retry is the only budget that is truly a night-level resource, and it is the one that multiplies by eight.

Current values, re-read:

| budget | value | real scope |
|---|---|---|
| `extract --limit 20 --run-budget-seconds 540 --ticket-budget-seconds 180` | `dream.sh:658` | **global** queue, 17 `pending` tickets measured |
| `roadmap --limit 10` | `dream.sh:698` | **10 projects**, not 10 features |
| `promote --limit 10` | `dream.sh:462` | 10 **candidates** from **one** project |

None of the three needs to be shared, for a different reason each time:

- `extract` and `roadmap` sit outside the loop: their budget stays literally the same.
- `promote --limit 10` **is never binding** — under today's filter. Replayed
  identically across the pool's eight projects, it gives: `red` 3 candidates, `red-shrik` 2,
  **the other six 0** — including `brain-v42`. The ceiling of 10 does not bite on anything.

  **This measurement is a day old, and that has to be said.** The predicate
  `access_count_human >= 3` went into `promote_prepare` with `62a91030`/`62633f88`
  (2026-08-06) and its ranking with `508439d2` (2026-08-08). Before that, `brain-v42`'s pool
  was **not** empty: `promote` ran as a real LLM phase every night from
  2026-07-18 to 2026-08-06 (`model=gpt-5.6-sol`, 15-19 s, ~75 000 tokens per night — measured
  in `dream_runs`). The first empty-pool night is **2026-08-07**, a single one. The "2
  projects out of 8" is therefore a snapshot, not a regime: it gets re-measured before step 6.

  **And the `record-empty-pool` line counter is not measurable: it is zero.**
  `SELECT count(*) FROM dream_runs WHERE phase='promote' AND model IS NULL` → **0 lines, across
  the table's entire history**. The block that calls it only went into `dream.sh` with
  `e8b59c3c` (2026-08-07), after that night's 06:00 run. The 2026-08-07 log shows it as a hollow —
  `[06:04:36] SKIP promote — empty candidate pool`, then directly
  `START reorg`, without the success line or the `WARN … row NOT recorded`:

  ```
  [06:04:36] SKIP promote — empty candidate pool
  [06:04:36] START reorg (…)
  …
  - promote [partial]: expected enabled phase missing from dream_runs
  ```

  The last line is **exactly the false alarm that `_record_empty_pool` was written
  to suppress**. So: the reference "one line per night today" does not exist, and the
  "6 lines per night" is not observed noise but a **prediction** about code that
  has never run in production. Consequence for this project: the pool's first night
  would also be the first verification of this path, multiplied by six. It must be **proven
  on one project** (a night with the `empty-pool dream_runs row recorded` line and the corresponding
  row in the database) **before** step 6 of §12.

**The retry is the real subject.** It costs +43 min per project (`dream.sh:539`: on hard failure
only, never on timeout, never on `promote`). At eight projects that is **+344 min of
ceiling**, i.e. the difference between 459 min (7,7 h) and 803 min (13,4 h).

Recommendation: **a retry allocation for the night, not per project.** With two
retries allowed for the night and the most expensive phase (synth, 15 min), the ceiling
becomes 8 × 53 + 2 × 15 + 35 = **489 min (8,2 h)**, against 803. That recovers 5,2 h of worst
case for a one-line mechanism.

**And the systemd ceiling must move BEFORE the loop, not after.** `TimeoutStartSec=10800`
(180 min) does not hold for two projects (227 min). It must be pegged to bound **(b)**, the
configured ceiling — not to the measured average of 80 min — because systemd kills at the
bound, not at the average. With the retry allocation: ~490 min, to round up.

**And it must move in the VERSIONED TEMPLATE, not the live unit.** The value is at
`deploy/systemd/brain-v42-dream.service.tmpl:41` and shows up at `:38` of the generated unit.
`deploy/systemd/install.sh` **regenerates the unit from the template**; its guard
(`:277-345`) only warns on `Environment=` lines added by hand — `TimeoutStartSec`
is not an `Environment=` line. A ceiling raised by hand in
`~/.config/systemd/user/brain-v42-dream.service` would therefore be **rewritten to 10800 by the
next reinstall, without a word**, and the following night would be killed at 180 min in the middle of the
third project. This is the twin of incident 2026-06-30 cited at the top of `killswitches.conf`
(PROMOTE+REORG switched off for two nights by a regeneration). The pool, on the other hand, belongs in the
drop-in: drop-ins survive regeneration.

### What we lose

- With a night-level allocation, the project at the head of the pool is better served than the one at the
  tail: if the first two burn through the allocation, the eighth is not retried. This is
  a real asymmetry. A natural mitigation, already proven in the repository: **rotate
  the pool's order**, as `rotate_keys` does for roadmap since 2026-07-04
  (`roadmap_curate.py:474-487`). Without rotation, it is always the same project that gets
  sacrificed.
- The configured ceiling stays well above the measurement (489 min of ceiling for 80 min
  of average and 126,6 min of p90). This is accepted: a ceiling serves the worst case. But it makes
  `TimeoutStartSec` useless as a practical guard — it is the p90 that must be watched,
  not the red unit.

---

## 11. Q6 — Eight alerts or one aggregated one?

### Decision: **a single aggregated alert, after the loop**, and the decorative `--project-key` is removed — replaced by a "project" line in the report body, which depends on 042.

**What the alert sends, and to whom: nothing, to no one.** Measured, not assumed.
`build_alert_insight` (`scripts/dream/post_run_alert.py:36-66`) builds a text string
of 20 lines at most. `_run` (`:138-151`) **prints it to stdout**. `dream.sh:782-784`
redirects that stdout `>> "$LOG_DIR/$TIMESTAMP.log"` — so it does not even go to the
systemd journal. No brain write, no webhook, no email. The name `write_alert_if_failed`
is a leftover: it writes nothing.

**Only its return code counts**, as the third condition of the exit gate
(`dream.sh:785` then `:803`) — the "mute reporter".

**Its `--project-key` has always been decorative.** The parameter is declared (`:158`),
passed to `write_alert_if_failed` (`:144`), received in its signature (`:129`)… and never read:
`fetch_failed_runs(session, run_date)` (`:90-123`) does not even receive it, and its three
queries filter on `dream_runs.c.run_date == run_date` alone. Eight invocations
would therefore produce **eight identical
blocks**, listing the failures of the eight projects, stacked in a file that §3.2 shows
will only survive for the last project anyway.

The only real recipient is the operator reading `logs/dream/<date>.log` in the morning. One
alert, once, grouped by project.

**Measured consequence to handle at the same time**: `MAX_REPORTED_FAILURES = 20`
(`post_run_alert.py:31`) was sized for 9 phases per night. At 51 lines, the ceiling
becomes reachable and the "N additional failure records omitted" line can hide
entire projects. Either raise it, or cap **per project** instead of the total.

### What we lose

- Nothing operationally: the per-project alert never existed. But the report becomes
  longer, and a single project's failure gets buried in a list. Grouping by project
  is what makes it readable — which is to say Q6 **depends on Q1**: without
  `dream_runs.project_key`, the aggregated report cannot label its lines.

---

## 12. Delivery order

The question asked was: *must the migration precede the loop, or can the
loop be shipped with mixed runs?*

### Answer: 042 precedes the loop. Non-negotiable, for a reason that is not aesthetic.

Three readers **write** to the line they misidentified: `promote_validate`
marks `partial` (`:181-196`) and **backfills `dream_promotions.dream_run_id`**
(`:122-127`); `connect_validate` marks `partial`; `REORG_RUN_ID` likewise. Shipping the loop
first would produce a **false and unrepairable promotions audit** — the
correct attribution is not recoverable from the lines once written. And `DISTINCT ON (phase)`
would silently erase failures for the entire interval.

Order:

| # | what lands | regime after merge |
|---|---|---|
| 1 | **The four hardcoded prompt lines** (`phase_synth.md:24,58`, `phase_promote.md:4`, `phase_connect.md:43`) + the missing `project_key` check in `promote_validate` | no change (single project) |
| 2 | **042** applied in production, `alembic current` **proven**; then the 5 writers and the 10 readers | no behavior change, one project, column filled |
| 3 | **Structural exit of the three global phases**, `project_key='*'`, test anchor | no change (they already ran once) |
| 4 | **Projected log paths** (§3.2), export reinit (§3.4), the `project/phase` counters (§9), `TimeoutStartSec` (§10) | no change to a single project |
| 5 | **The loop**, `BRAIN_DREAM_PROJECT_POOL` absent from the drop-in → `brain-v42` alone, plus Cartesian `expected_dream_phases` (§6) | **identical to today, byte for byte** |
| 6 | **Opening the pool, one key at a time**, measuring the night with each addition | regime change, reversible by removing one word |

Steps 1 through 5 are shippable without a single night changing behavior. This is the
property that makes this project safe, and it is worth preserving at every commit.

**Ordering constraint between the database and the merge**: `dream.sh` runs from the
**root** working tree, so merging into `main` is deploying. 042 must be **applied in
production before** the merge that introduces the readers referencing the column, otherwise the
following night breaks. And the revision number gets **measured**, it is not copied over from one
session to another — CLAUDE.md carries the incident where "production stays at 037" was
asserted for three days after the cutover.

---

## 13. What ships with the killswitch closed

Repository doctrine: every new capability ships closed, because **committing to a
branch is already deploying** as soon as it reaches `main`.

- **`BRAIN_DREAM_PROJECT_POOL` is not added to the drop-in.** `dream.sh`'s default is
  `brain-v42` alone, exactly like the current positional argument (`:70`). The loop
  runs with one element. Identical behavior, byte for byte.
- **No existing killswitch changes value.** Not PROMOTE, not REORG, not EXTRACT, not
  ROADMAP, not SWEEP. The pool is not an occasion to revise a cadence.
- **`sweep` stays `ENABLED=false`, `DRY_RUN=true`.** It has never written a line into
  `dream_runs`; its soak is independent of this project (§8).
- **`BRAIN_DREAM_CAPABILITY_ENFORCEMENT` stays absent**, hence `false` (§14). This project
  does not arm it.
- **042 is not a killswitch.** It is nullable and without backfill precisely so
  that a non-migrated reader and a migrated writer coexist without breaking. Applying it stays
  an out-of-band operator step.
- **The install gate test changes at the same time as the template.**
  `tests/integration/test_dream_systemd_install.sh:180` pins
  `ExecStart=… dream.sh brain-v42`, and `:284,286` pin
  `--preflight-capabilities --project-key brain-v42` with exactly 3 phases. These
  assertions are the proof that the live unit has not drifted; they are not to be worked
  around, they get updated.

---

## 14. What stays OPEN — and this is the most important point

### 14.1 The server scope exists, it is written, tested, and **off**

`src/brain_v42/services/dream_project_scope.py` and
`src/brain_v42/mcp/dream_capabilities.py:224-261` (`on_call_tool`) implement a full
middleware: a `scoped` principal carries `(phase, project_key)`,
`authorize_dream_project_request` injects `project_key` into tool calls per
`PROJECT_TOOL_POLICIES` — the table lives at `services/dream_project_scope.py:83-120`, re-exported
by `mcp/dream_project_authorization.py` — and `bind_dream_project_scope` makes it visible to
handlers. **19 sites** in `src/` call `get_dream_project_scope()` (14 more in
`tests/`).

It is **inert in production**: `scripts/dream.sh:20` reads
`BRAIN_DREAM_CAPABILITY_ENFORCEMENT="${BRAIN_DREAM_CAPABILITY_ENFORCEMENT-false}"`, and the
variable is **absent** from the `.env` as well as from all three systemd drop-ins (`killswitches.conf`,
`nvidia.conf`, `token.conf`) — verified. The principal stays `unscoped`, all tools
operate on the 54 projects.

**This is §4.1's real lever.** As long as it is off, looping eight times spends 80 min
to produce the 15-min result. But arming it is not enough: `brain_decay_status`,
`brain_consolidation_candidates`, `brain_backfill_links_batch`,
`brain_list_orphans_for_classification` and `brain_get_clusters` carry an
**empty** `DreamProjectToolPolicy()` — no scope, even with the middleware on. And these are
exactly the five tools that SCAN, CLEAN, CONNECT and SYNTH use.

**Question left open, deliberately**: does the pool make sense before these five
policies are filled in? This spec does not settle it, because the answer depends on a
measurement that does not exist — the duration of an agent run truly scoped to one project. It must
be put to the operator **before step 6** of §12, not after.

### 14.2 Run provenance identity

`scripts/dream/codex_runner.py:244` computes `agent_header = f"dream-codex-{phase}"` and `:262`
sets it as `X-Brain-Agent` — with no project.
The eight runs would be indistinguishable in `access_log.actor`, the column that 041
added precisely to make this visible.

But putting the project into the actor **breaks a measured ceiling**:
`src/brain_v42/metrics/collector.py:130` sets `_MAX_AGENTS = 32`. The dream occupies 6 slots
today; `dream-codex-{phase}-{project}` would occupy **48** — over the ceiling by
itself, and it would evict human actors. Two side constraints verified:
`provenance.py:27` `_SYSTEM_ACTOR_PREFIXES = ("dream-codex-",)` would still classify these
actors as "system", so `access_count_human` would not be skewed; and `access_log.actor`
is `VARCHAR(64)`, where `dream-codex-promote-auto-discord` (32 characters) fits.

**Open**: not changing the actor in this batch is the default recommendation, but
that leaves run provenance blind to the project while `dream_runs` knows it.
The two views will diverge.

---

## 15. Tests

TDD, Red-Green-Refactor cycle like the rest of the project. Baseline measured over the dream radius:

```
pytest tests/unit -q -k "dream or promote or reorg or connect_validate or roadmap
                         or extract or sweep or preflight or render_prompt"
→ 952 passed, 47 skipped, 6309 deselected, 22,30 s
```

What the pool must add, one test = one behavior:

- **042 nullable, never blocking**: a best-effort INSERT with no `project_key` still
  succeeds. The test must go through the `except Exception: print(warning)` path of
  `ticket_extract.py:764`, otherwise it proves nothing.
- **Sentinel for the globals**: `extract`, `roadmap` and `sweep` write `'*'`; a loop
  phase writes a project key; `NULL` is written by no one.
- **`roadmap`'s `_clean_dry_streak` survives the cutover**: with 856 lines at `project_key
  NULL` plus new lines at `'*'`, today's measured counter (24) must not
  fall back to 0. It is the only streak worth anything; it is the one that must be
  pinned.
- **`DISTINCT ON (phase)` no longer erases**: two `synth` lines the same night, a `fail` on
  one project and a `done` on another → the `/dream` payload must show the failure.
- **Cartesian `expected_dream_phases`**: a pool project that skips `promote` triggers
  the alarm **even if** the other seven observed it. The test must simulate the other
  seven, otherwise it goes green without proving anything.
- **Project-major order**: project N's CONNECT receives project N's CLEAN report, not
  project N−1's. Textual anchor on the loop structure, like
  `test_dream_sh_agent_provider.py:74`.
- **Export reinitialization**: a project that skips PROMOTE does not leave
  `PROMOTE_CANDIDATE_POOL_JSON` loaded for the next one.
- **Isolation**: project 3 fails, projects 4 through 8 still run, and
  `FAILED_PHASES` contains `projet3/synth` — not `synth`.
- **Three global phases, exactly once**: with a pool of 3 projects, exactly one
  `extract` line, one `roadmap`, one `sweep`.
- **Disjoint log paths**: two projects do not mutually truncate each other's
  report. To write against `codex_runner.py:419,432-433`, which is the place that truncates.
- **SHARED log paths, conversely**: `$TIMESTAMP.log` and the three global-phase logs
  stay **unprojected** (§3.2). Without this test, the "project it" instruction will be
  applied to all twelve and manufacture seven empty files plus a fragmented night narrative.
- **Pool format, against the real drop-in**: a two-project pool written into an
  `Environment=` is read back **with both its projects**. The test must go through the real
  drop-in text, not a Python list already split — otherwise it proves nothing about systemd's
  whitespace-splitting trap (§6).
- **`record-empty-pool` really writes its line**: empty pool → one `dream_runs` line
  `phase='promote'`, `model IS NULL`, with the current project's `project_key`, AND the
  success log line. This path has **never** run in production (§10); the test is its
  first proof.
- **`fetch_failed_runs` groups by project**: two projects failing the same night produce
  two labeled lines, and the `MAX_REPORTED_FAILURES` ceiling does not erase an entire project.

To update at the same time: `tests/unit/test_dream_sh_agent_provider.py:74,89` (two
literal assertions on the script's text) and
`tests/integration/test_dream_systemd_install.sh:180,284,286`. The 8 shell integration
tests **are not collected by pytest** (`testpaths=["tests"]` does not pick up `.sh`
files) — a note already recorded in `test_dream_sh_exit_code.py:60-64`, which means they
run by hand or not at all.

---

## 16. Accepted limits

- **479 out-of-scope sub-project artifacts**, including `red-shrik:agent` (280) bigger
  than its parent (222). The pool covers **62,4 %** of the corpus (2 373 / 3 803); **37,6 %**
  stay outside (1 430), including 26 artifacts with no `project_key` at all. No prefix
  mechanism exists to close that gap (§2).
- **As long as the server scope is off, eight runs do eight times the same work.** The
  pool buys the infrastructure for multi-project consolidation, not multi-project
  consolidation itself (§14.1).
- **The wall-clock range rests on the assumption "per-run cost independent of the
  project"**, true today and false the day the scope is armed. p90 126,6 min and max
  286,3 min are simulations over real nights, not measurements of an eight-project
  night — which has never taken place.
- **The pre-flight will bound nothing.** `dream_preflight._fetch_signals:98-115` aggregates the five
  tables **with no project filter**, and the gate has only triggered **2 times out of 121
  nights** (1,7 %). With the pool, the verdict becomes shared by all eight: a write in
  `red` will run the Opus phases for `red-watcher` (31 artifacts).
- **The logs have no rotation at all.** Measured in production: `logs/dream/` contains
  **1 865 files for 121 Mo**, and `grep logrotate|mtime|find.*logs` against `dream.sh` and
  `deploy/systemd/install.sh` gives **no result**. The pool multiplies the file rate
  by 8. This is not a problem of this spec, but it moves up its deadline.
- **The XML scrub will run 8 times a night.** `scripts/scrub_xml_tool_call_leak.py:122-132`
  sweeps `learnings`, `decisions` and `project_contexts` in full, with no project filter.
  Idempotent → cost only, no corruption. Same for the Codex pre-flight in `dream.sh:363-387`.
- **Up to 6 `record-empty-pool` lines per night — predicted, not measured.** Production has
  written **zero** to date; the path only went into `dream.sh` on 2026-08-07, after the
  run. The only empty-pool night observed (2026-08-07) produced no line and **triggered
  the synthesis `partial`** that this mechanism was meant to switch off (§10). To be proven on one
  project before making it six.
- **`sweep` stays global and outside the pool**, so "the pool" and "what `sweep` touches" are
  two different sets forever (§7, §8).
- **No failure quorum.** A failure on the smallest project reddens the unit exactly as much as seven
  on the biggest (§9).

---

## 17. What I could not measure

Written here rather than estimated elsewhere.

1. **The maximum throughput of the subscription that authenticates Codex.** ~11.6 M tokens/night on
   average and ~30.8 M on the worst night are extrapolations from `dream_runs`; nothing
   here says whether the subscription's ceiling absorbs them. This is the most serious
   unquantified risk of this project.
2. **`extract` and `roadmap`'s NVIDIA API budget.** `dream_runs` records `0` tokens
   for these two phases (verified): their real consumption is not measurable from the
   database. Since both sit outside the loop, exposure is zero in the chosen
   design — but it would be invisible in another one.
3. **The number of distinct human actors** against the `_MAX_AGENTS = 32` ceiling:
   `access_log` contains **0 lines** in production at the time of measurement.
4. **The real duration of an agent run on a non-`brain-v42` project.** Never executed.
   The "constant cost per run" assumption (§4.1) is deduced from the structure of the prompts and
   tool signatures, not from a measurement.
5. **The corpus's behavior at eight SYNTHs per night.** The feedback loop — SYNTH
   creates `dream:generated`, excluded from the pre-flight signal by `dream_preflight.py:78` — has
   never been observed at this rate.
6. **Consumers of the `/metrics` payload outside the repository** (red-monitor): not inspected,
   so the real impact of `DISTINCT ON` readers on their dashboards is not
   established.
7. **The Neo4j-side cost** of eight passes of `brain_get_clusters` and
   `brain_backfill_links_batch` per night.
8. **The behavior of `record-empty-pool`**, which has never written a line (§10). Everything
   the spec says about it is deduced from the code, not observed.
9. **The real regime of the PROMOTE pool.** The "2 projects out of 8 non-empty" measurement is **one
   day** old, under a maturity predicate entered on 2026-08-06 and a ranking entered
   today. The earlier series (non-empty pool every night for `brain-v42`) is measured,
   but it describes a different filter. To be re-measured before step 6.
10. **The red-night rate under an open pool.** §9 gives a projection
    (`1 − (1 − p)⁸` → 42-77%) from two measured rates, under an
    independence assumption that is false in the conservative direction. No night with more than one project
    has ever run.

## 18. Out of scope

- **`scripts/dream/cross_project_resonance.py`** — no call site in `scripts/`,
  `src/` or `deploy/` (verified), killswitch `BRAIN_DREAM_CROSS_PROJECT_ENABLED=false`. Not to
  be confused with the pool: cross-project resonance reads several projects within one run,
  the pool runs one run per project.
- **Including the six sub-projects** — debt dated and quantified (§2), separate ticket.
- **Arming `BRAIN_DREAM_CAPABILITY_ENFORCEMENT` and filling in the five
  empty policies** — separate project, and probably a priority over opening the
  pool (§14.1).
- **Opening `sweep` in WET** — independent decision, never in the same batch (§8).
- **Log rotation** — pre-existing problem this does not close.
- **The project in `X-Brain-Agent`** — blocked by `_MAX_AGENTS` (§14.2).

---

## 19. Adversarial review — what was re-measured

On 2026-08-08, every figure in this spec was replayed against production.

**Reproduced identically**: the pool and sub-project masses (2 373 / 3 803 / 479);
the wall-clock simulation across its eight decimals (15,05 · 21,22 · 72,76 · 80,04 ·
126,62 · 286,26 · 120,41 · 582,10); the breakdown by phase; the tokens' `NULL` trap
(167 lines out of 272, 1 455 688 / 3 850 807, 0,6425 $ / 4,6846 $); the five DRY streaks, of which
`roadmap` = 24; the 7 ghosts at 7 days and their 3-in-the-pool / 4-outside split, zero
for `brain-v42`; the PROMOTE pool (`red` 3, `red-shrik` 2, six others 0); the 17 `pending`
tickets; `access_log` at 0 lines; `logs/dream` at 1 865 files for 121 Mo; the pre-flight
at 2 triggers out of 121 nights; the pytest baseline 952/47/6309; the four hardcoded
prompt lines; the live drop-in's regime; `TimeoutStartSec=10800`; the 21 tests and the
textual anchors of `test_dream_sh_exit_code.py`.

**Corrected**: `roadmap`'s rotation (30 projects and 3 nights, not 54 and 6); the
`record-empty-pool` counter (zero lines in production, not one per night — §10); the one-day
staleness of the PROMOTE pool measurement; the path-projection instruction (7 out of 12, not 12 out of 12
— §3.2); the pool's transport in `Environment=` (systemd whitespace splitting, boolean
parser — §6); where to move `TimeoutStartSec` (the versioned template, not the live
unit — §10); the expected red-night rate (§9); the share outside the pool (37,6 %); the number of
scope sites (19, not 21); the line numbers of the five `continue`s, of `post_run_alert` and
of `codex_runner`; two string lengths (16 and 32 characters).

**Not overturned**: none of the six recommendations. Q1 (042 nullable, `'*'` sentinel), Q2
(global killswitches), Q3 (the three global phases outside the loop), Q4 (isolation without quorum),
Q5 (per-project budgets, night-scale retries) and Q6 (one aggregated alert) all hold
after counter-argument. Q4 and Q6 arrive with a now-explicit measurement debt; Q2 arrives
with two transport traps that were not visible before.
