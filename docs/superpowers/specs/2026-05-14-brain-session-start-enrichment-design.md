# brain_session_start Enrichment — Design

**Date:** 2026-05-14
**Status:** Brainstorm sections 3-7 (1-2 closed: scope + Approach B validated)
**Related:** learning 9a677c1a (killswitch-as-cadence-decoupling), decision 7a91a142 (REORG sub-flag), critique synthesis 2026-05-13

---

## 1. Scope (closed)

Replace the current focus + recent-decisions + recent-learnings briefing with an **action-forward** layout that surfaces what the next session needs to *act on* before what it needs to *know about*. Briefing token budget unchanged (~500-800).

In: `brain_session_start(project_key)` output reshape; minimal schema additions; per-section data sources.
Out: cross-project briefing (covered by Spec C MVP plan 697d5b7c); UI changes; new MCP tools.

## 2. Approach B (closed)

Action-forward order, top to bottom:

```
killswitches → last-failure → in-flight → stale-pinned → focus → blockers → recap
```

Critique synthesis (3 judges) decisions baked in:
- **Drop:** emojis, quoted titles, absolute dates in headers, `pinned` tag literal, `id:` prefix
- **Add:** killswitches block, last-failure block, blockers block, stale-pinned block, drill-in hint at bottom
- **Soak-progress data source:** option (c) — `phase_dry_run BOOL` per phase row on `dream_runs`

## 3. Briefing schema

### 3.1 Output skeleton

```
## {project_key} — Session Briefing

### Killswitches (as of {last_run_date})
- PROMOTE: {enabled|disabled} ({wet|dry} · {N} clean DRY nights)
- REORG:   {enabled|disabled} ({wet|dry} · {N} clean DRY nights)
- GRAPH:   {enabled|disabled}

### Last failure
{phase} on {YYYY-MM-DD} — {error_message_first_line}
→ drill in: brain_get(decision, …) or journalctl -u brain-v42-dream

### In-flight ({N})
- {feature.title} [{status}] — updated {Nd ago}

### Stale-pinned ({N})
- {entity_type}: {title} — pinned, last access {Nd ago}

### Focus
{ctx.current_focus}

### Blockers ({N})
- {blocker_text}

### Recap
- d: {decision.title}
- l: {learning.topic}: {first_60_chars}…

→ More: brain_search · brain_get_roadmap · brain_list types=…
```

### 3.2 Display rules

- Sections with zero items are **omitted**, except `Killswitches` and `Focus` (always-show anchors).
- Dates rendered as relative ("3d ago", "today"), never absolute, except killswitch freshness which always shows the YYYY-MM-DD of the last dream run.
- Titles rendered raw (no surrounding quotes).
- IDs never inline; truncate to drill-in instructions at the bottom.
- Per-section line cap: killswitches=3, in-flight=5, stale-pinned=5, blockers=5, recap=3+3.
- Recap items prefixed `d:` / `l:` (1 char + colon) to keep type signal without taking width.

## 4. Data sources

| Section | Source | Query |
|---------|--------|-------|
| Killswitches | `dream_runs` last 24h | derive: PROMOTE row exists ⇒ enabled; REORG row + `phase_dry_run` shape; freshness = `MAX(run_date)` |
| Last failure | `dream_runs WHERE status='failed'` | `ORDER BY created_at DESC LIMIT 1`, only if within 7d |
| In-flight | `features WHERE status IN ('in_progress', 'planned') AND project_key=?` | `ORDER BY updated_at DESC LIMIT 5` |
| Stale-pinned | `features WHERE pinned=true AND updated_at < NOW()-30d AND project_key=?` | `ORDER BY updated_at ASC LIMIT 5` |
| Focus | `project_contexts.current_focus` | already loaded |
| Blockers | `ctx.blockers list[str]` (already loaded with project_context) | render each item, cap 5 |
| Recap | existing decision_svc + learning_svc paths | unchanged, limit reduced 5→3 each |

### 4.1 Killswitch derivation logic

For the last `MAX(run_date)`:

```python
phases = {row.phase: row for row in dream_runs_today}
PROMOTE_enabled = 'promote' in phases  # phase ran ⇒ killswitch on
REORG_enabled  = 'reorg'   in phases
REORG_dry      = phases.get('reorg', None) and phases['reorg'].phase_dry_run
PROMOTE_dry    = phases.get('promote', None) and phases['promote'].phase_dry_run
```

Clean-DRY-nights counter:
```sql
SELECT COUNT(*) FROM dream_runs
WHERE phase=$1 AND status='ok' AND phase_dry_run=true
  AND run_date > (
    SELECT COALESCE(MAX(run_date), '1970-01-01')
    FROM dream_runs WHERE phase=$1 AND (status<>'ok' OR phase_dry_run=false)
  )
```
Resets on first failure or first WET run.

GRAPH state read from `os.getenv("GRAPH_ENABLED")` (MCP-process-local, no derivation needed).

## 5. Implementation surface

### 5.1 Schema migration

```sql
-- migration 022 (next free)
ALTER TABLE dream_runs
  ADD COLUMN phase_dry_run BOOLEAN NOT NULL DEFAULT false;
```

That is the **entire** new schema. No new table, no new index, default-false makes back-fill a no-op (legacy rows treated as WET because they had no sub-flag — and historically WET was the only mode for non-PROMOTE/REORG phases).

### 5.2 dream.sh change

Pass `phase_dry_run` to the OTEL parser when inserting each `dream_runs` row:
- `phase_dry_run = ($DRY_RUN OR ($name == 'reorg' AND $REORG_DRY_RUN))` for that phase
- pure-WET phases (SCAN/CLEAN/CONNECT/SYNTH today) get `false`

### 5.3 session_tools.py refactor

`_format_session_briefing` becomes a composer:

```python
def _format_session_briefing(ctx, recent_decisions, recent_learnings,
                             killswitches, last_failure, in_flight,
                             stale_pinned) -> str:
    sections = [
        _section_header(ctx),
        _section_killswitches(killswitches),         # always
        _section_last_failure(last_failure),         # omit if None
        _section_in_flight(in_flight),               # omit if []
        _section_stale_pinned(stale_pinned),         # omit if []
        _section_focus(ctx),                         # always
        _section_blockers(ctx.blockers),             # omit if []
        _section_recap(recent_decisions, recent_learnings),
        _section_drill_in_hint(),
    ]
    return "\n\n".join(s for s in sections if s)
```

`brain_session_start` adds 4 service calls in parallel (gather): killswitch derivation, last_failure, in_flight, stale_pinned. Blockers reads from already-loaded `ctx.blockers` — no extra query. Currently 2 calls (decisions + learnings) — net +4 queries, all single-row or small-LIMIT.

### 5.4 New service surface

- `dream_run_svc.killswitch_state(project_key) -> KillswitchState` (NEW)
- `dream_run_svc.last_failure(project_key, within_days=7) -> DreamRun | None` (NEW)
- `feature_svc.in_flight(project_key, limit=5)` (NEW or extend list_all)
- `feature_svc.stale_pinned(project_key, stale_days=30, limit=5)` (NEW)

Blockers source = `ctx.blockers` (`list[str]`, already on `ProjectContext`). No new service method.

All async, all single-statement SQL. Estimated +100 lines of service code, +60 of `_section_*` helpers.

## 6. Edge cases & open questions

### 6.1 No project_context

Current behavior preserved: header reads "Session Briefing (no project context found)" and only Recap renders. Killswitches and Focus skipped (the only two always-shown sections require ctx).

### 6.2 No dream_runs in last 7d

Killswitches block renders `### Killswitches (no dream pipeline activity in 7d)`. Counters not shown. Drives the user to investigate timer health rather than masking the silence.

### 6.3 Blockers source (closed)

Uses the existing `ProjectContext.blockers list[str]` field — already populated via `brain_set_project_context(..., blockers=[...])`. No tag convention, no new query, no schema change, no CLAUDE.md doc. The briefing simply renders the list.

### 6.4 Stale-pinned threshold (closed)

Locked at **30 days**, pure age-based: `features WHERE pinned=true AND updated_at < NOW() - INTERVAL '30 days'`.

Rationale for not reusing decay: `services/decay.py` profiles only `decision/learning/snippet/runbook/adr`. Features are not in the decay registry — there is no decay multiplier to reuse. If decay coverage is ever extended to features, this threshold becomes a candidate for unification, but that's a separate proposal.

### 6.5 Drill-in hint placement

Putting it at the bottom risks being below the cache-cutoff in long briefings. If the briefing grows past 800 tokens, hoist the hint above Recap. Cap-aware composer.

## 7. Tests

TDD per §CLAUDE.md. Strict RED-GREEN-REFACTOR.

### 7.1 Unit (per section builder)

One test file per `_section_*` helper. Each:
1. RED: empty input → expected omission (or anchor render for killswitches/focus)
2. RED: full input → expected formatted block (golden string)
3. RED: cap enforcement (N+1 items → N rendered + nothing else)

Estimated 18 unit tests (3 per section × 6 sections).

### 7.2 Killswitch derivation

Separate test class on `dream_run_svc.killswitch_state`:
- mixed phase rows (some with `phase_dry_run=true`) → correct DRY/WET classification
- no rows in 7d → `(no activity)` shape
- consecutive DRY clean nights → counter increments
- one failure resets counter
- one WET run resets counter

5 tests.

### 7.3 Integration / golden snapshot

`tests/integration/test_session_start_briefing.py`:
- Seed: 1 project_context, 3 dream_runs (PROMOTE WET ok / REORG DRY ok / SCAN WET ok), 1 in-flight feature, 1 stale-pinned feature, 1 blocker decision, 5 recent decisions, 5 recent learnings.
- Assert: full briefing matches golden file `tests/fixtures/briefing_full.md`.
- Second case: empty seed → degraded briefing matches `briefing_empty.md`.

2 integration tests + 2 golden fixtures.

### 7.4 Token budget regression

Assert `len(briefing) < 4000` (≈800 tokens × 5 chars/token, generous). Backstop against schema/fixture drift bloating the output.

1 test.

**Total new tests: ~26.** All pure SQL or in-memory composition, no LLM, no Neo4j — fast unit suite.

---

## 8. Implementation surface summary

| Surface | Net change |
|---------|------------|
| Schema | +1 column (migration 022) |
| dream.sh | +1 expression for phase_dry_run, +1 OTEL parser arg |
| session_tools.py | refactor _format_session_briefing into composer + 6 helpers (~80 LOC) |
| Services | +4 methods (~100 LOC) |
| Tests | +26 tests (~600 LOC) |

No new MCP tools. No new tables. No graph changes. No CLAUDE.md change. Migration is roll-forward safe (default false).

## 9. Sequencing

1. Migration 022 + dream.sh wiring + dream_runs.phase_dry_run population (TDD)
2. Service-layer methods (TDD)
3. Section helpers + composer refactor (TDD)
4. Golden snapshot fixtures + integration tests
5. Manual smoke: `brain_session_start brain-v42` against live PG, eyeball output

Each step independently shippable. No big-bang release. Killswitches: none needed — additive change, fallback path is the legacy briefing if any composer call raises (graceful degrade with structlog warn).

## 10. Out of scope (explicit)

- Cross-project briefing variant — covered by Spec C MVP plan 697d5b7c.
- Per-user customization (which sections to enable) — premature.
- Token-budget aware truncation engine — current cap-per-section + total-cap test is sufficient for v1.
- Killswitch-write tool (so the briefing could also flip flags) — mixes read and write, separate proposal.

---

**Next:** user review of this spec → invoke `superpowers:writing-plans` to convert into batch-annotated implementation plan.
