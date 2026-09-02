# Spec C MVP β — Cross-Project Briefing & Resonance Detection

**Date**: 2026-05-01 (v2 post-multi-judge critique)
**Status**: Implemented 2026-06-12 (plan: docs/plans/2026-06-12-spec-c-cross-project-resonance.md) — killswitch closed, rollout pending
**Branch**: `feat/dream-cross-project-resonance-and-briefing`
**Killswitch**: `BRAIN_DREAM_CROSS_PROJECT_ENABLED=false` (closed by default)

## Context

The brain MCP (brain-v42) accumulates knowledge per-project. Today, the Layer-2 domain registry (9 closed domains: infra, ml, backend, memory, tooling, data, ops, frontend, security) is operational and already global at the Neo4j level (`Domain {name}` is not keyed on `project_key`). The PG repos already accept `project_keys: list[str]` for multi-project filtering. But the user (human or Claude) has no surface that exploits this cross-project topology.

The previous Spec C (full) covered 4 angles. This MVP β scopes only two angles, chosen for their primary benefits:
- **Knowledge transfer**: surface relevant insights from other projects when working on the current project
- **Resonance detection** (drift OR convergence candidates): detect cross-project, intra-domain decision pairs at high cosine, the algorithm does not settle the verdict (human interprets)

## Goals & Non-Goals

### Goals (MVP)
1. Enrich `brain_session_start` with a "Cross-project insights" section listing the top-N entities from other projects in the current project's active domains.
2. Add a script `scripts/dream/cross_project_resonance.py` that produces a nightly, DRY_RUN-able markdown report of cross-project decision pairs at cosine >= threshold.
3. Killswitch closed by default. No write without an explicit flag + env var.
4. No new MCP tool. No DB or Neo4j schema change.
5. Existing test suite (1837) stays green. New code covered by TDD.

### Non-Goals
- No `brain_search` runtime change (option α/δ — reserved for a future iteration if the MVP works).
- No `brain_search_cross_project` MCP tool nor equivalent (user constraint).
- No automatic brain_learn write: DRY_RUN by default, WET mode requires human review over ≥ 5 nights.
- No inline cross-project warning in `brain_log_decision` (future γ/δ).
- No cron integration (manual first, cron in a separate follow-up).
- No integration tests for the MVP (unit tests suffice).

## Preconditions (verified during exploration)

The spec assumes the following invariants. If one breaks, an explicit guard is needed (TBD during planning):

| Invariant | Verified | If KO |
|-----------|---------|-------|
| `decisions.embedding` is `Vector(1536)` but can be NULL (cf. `pg_decision.py:245`) | ✅ | The script MUST filter `WHERE embedding IS NOT NULL` |
| All cross-project Decisions were embedded with **Qodo-Embed-1.5B** (1 single model in the codebase, cf. CLAUDE.md) | ✅ | If the model ever changes: full recompute needed — out of MVP |
| Global Neo4j Domain nodes keyed by `name` alone (cf. `graph_service.py:299`) | ✅ | — |
| `BELONGS_TO_DOMAIN` edges created via Layer-2 (agent-driven CONNECT step B) | ✅ | Entities not yet classified are silently excluded from scope — acceptable behavior |
| `ALLOWED_DOMAINS` constant exportable from `services/graph_service.py` | ✅ | Reused as-is in the script |
| `Project {project_key}` Neo4j node exists for each active project | To verify during planning | Cypher returns 0 results if missing — briefing section omitted (graceful) |
| Thresholds module: `src/brain_v42/thresholds.py` with `by_name(name) -> ThresholdSpec | None` (cf. line 146) | ✅ | Spec uses `by_name("cross_project_resonance_min").value` |
| Native pgvector `<=>` operator on `decisions.embedding` (cf. `pg_decision.py:247`) | ✅ | Pair-cosine done in PG, not in Python (cf. architecture decision below) |

## Architecture

### Modified components
- `src/brain_v42/mcp/tools/session_tools.py` — `_format_session_briefing` gains an optional `cross_entries` param. New private function `_fetch_cross_project_entries(...)` that queries Neo4j + PG.
- `src/brain_v42/services/graph_service.py` — Two new methods: `fetch_active_domains(project_key, top_n)` and `fetch_cross_project_entity_ids(domains, exclude_project_key, limit)` (returns only IDs + types + project_key, not the bodies).
- `src/brain_v42/repositories/pg_decision.py` — New method `fetch_with_embeddings_by_ids(ids: list[UUID]) -> list[DecisionWithEmbedding]` (pair compute) + `fetch_brief_by_ids(ids: list[UUID]) -> list[DecisionBrief]` (briefing display).
- `src/brain_v42/repositories/pg_learning.py`, `pg_snippet.py`, `pg_runbook.py`, `pg_adr.py` — Symmetric `fetch_brief_by_ids(...)` for briefing display (1 round-trip per type).
- `src/brain_v42/thresholds.py` — A new `cross_project_resonance_min` entry in `REGISTRY` (value=0.80, calibrated=False).
- `src/brain_v42/config.py` — Three new env vars + a reader helper.

### Created components
- `scripts/dream/cross_project_resonance.py` — CLI script `python -m scripts.dream.cross_project_resonance [--mode dry_run|wet] [--domains ml,memory] [--date YYYY-MM-DD]`.
- Corresponding unit tests in `tests/unit/`.

### Untouched components
- PG schema (decisions, learnings, snippets, runbooks, adrs) — no migration.
- Neo4j schema (Domain, Project, BELONGS_TO, BELONGS_TO_DOMAIN) — no change.
- 30 existing MCP tools — no addition, no public API change.
- `brain_search` — untouched in the MVP.
- `dream_runs` table — not modified but the script uses `INSERT INTO dream_runs (...) RETURNING id` for traceability (existing pattern).

### Data flow

**Briefing (`brain_session_start`)**:
```
client → brain_session_start(project_key)
  → project_context_svc.get_by_key(project_key)
  → decision_svc.list_all(project_key, limit=5)
  → learning_svc.list_all(project_key, limit=5)
  → IF env CROSS_PROJECT_ENABLED:
      → graph_service.fetch_active_domains(project_key, top_n=cfg.TOP_N)  # default 2
      → graph_service.fetch_cross_project_entity_ids(domains, exclude=project_key, limit=cfg.MAX)  # default 5
      → grouped_ids = {Decision: [...], Learning: [...], ...}
      → cross_entries = []
      → FOR (entity_type, ids) in grouped_ids:  # 1 PG round-trip per type
          → cross_entries.extend(repo_for(entity_type).fetch_brief_by_ids(ids))
      → cross_entries.sort(key=created_at desc)
    ELSE: cross_entries = None
  → _format_session_briefing(ctx, decisions, learnings, cross_entries)
  → return markdown
```

**Resonance script (`cross_project_resonance.py`)**:
```
CLI invocation
  → check env CROSS_PROJECT_ENABLED (else exit 0 fast, log "disabled")
  → INSERT INTO dream_runs (kind='cross_project_resonance', mode=$mode, started_at=now()) RETURNING run_id
  → threshold = thresholds.by_name("cross_project_resonance_min").value  # 0.80
  → target_domains = --domains or ALLOWED_DOMAINS  # 9
  → all_pairs: list[ResonancePair] = []
  → FOR domain IN target_domains:
      → entity_ids = graph_service.fetch_decision_ids_in_domain_across_projects(domain)  # Neo4j filter
      → IF len(entity_ids) < MIN_DECISIONS_PER_DOMAIN: skip  # 5
      → pairs = pg_decision.fetch_cross_project_resonance_pairs(  # PG-side cosine via <=>
            ids=entity_ids,
            threshold=threshold,
            limit=MAX_DECISIONS_PER_DOMAIN  # 200, hard cap to bound cost
        )
      → all_pairs.extend(pairs annotated with domain)
  → all_pairs.sort(key=cosine desc)[:MAX_PAIRS_PER_NIGHT]  # 20
  → write_markdown_report(path = artifacts/dream/cross_project_resonance_<UTC-date>.md)  # overwrite if exists
  → IF --mode wet:
      → IF NOT env_enabled(): log_error + exit 1 (defensive double-check)
      → FOR pair IN all_pairs:
          → dedup_key = sha256(f"{min(a_id, b_id)}|{max(a_id, b_id)}|{domain}")
          → IF learning_repo.exists_by_dedup_key(dedup_key): skip (idempotency)
          → learning_repo.create(
                topic=f"cross_project_resonance/{pair.domain}",
                insight=pair.format_insight(),
                tags=["dream", "cross_project_resonance", pair.domain, "EXCLUDE_FROM_PROMOTE"],
                project_key="brain-v42",
                source_kind="cross_project_resonance",  # for PROMOTE/CONSOLIDATION exclusion filter
                dedup_key=dedup_key,
                dream_run_id=run_id,
            )
  → UPDATE dream_runs SET ended_at=now(), pair_count=$N WHERE id=$run_id
  → exit 0
```

### Feedback-loop insulation (WET mode)

Risk identified (J2): `brain_learn(project_key="brain-v42", ...)` re-enters the PROMOTE pipeline then CONSOLIDATION, which could merge resonance learnings with each other and corrupt the genealogy.

MVP mitigation:
- **`EXCLUDE_FROM_PROMOTE` tag** + **`source_kind="cross_project_resonance"`** on every emitted learning
- PROMOTE phase (existing `promote_prepare.py:fetch_candidates`) **MUST** filter `WHERE source_kind != 'cross_project_resonance' AND 'EXCLUDE_FROM_PROMOTE' NOT IN tags` — **the PROMOTE modification is INCLUDED in this MR** (otherwise WET stays blocked forever)
- Tests: `test_promote_excludes_cross_project_resonance_learnings`

If the `source_kind` column does not exist on `learnings`: use only the `EXCLUDE_FROM_PROMOTE` tag as single-source-of-truth (and adapt `fetch_candidates` accordingly). Final decision at implementation planning time after schema verification.

## Detailed Design

### A. `brain_session_start` cross-project briefing

**Additive format** (after the current block, separated by a blank line):

```
**Cross-project (ml, memory):**
- [red-shrik] Decision · 2026-04-28 · embedding healthcheck pattern
- [red-shrik] Learning · 2026-04-22 · cosine 0.85 retient les vrais clusters
- [red-monitor] Learning · 2026-04-15 · go-pubsub close channel race
```

**Display field mapping** (per entity type):

| Entity type | Display field | Truncation | Notes |
|-------------|---------------|------------|-------|
| Decision | `title` | 60 chars + `…` | — |
| Learning | `topic` | 60 chars + `…` | — |
| Snippet | `intent` | 60 chars + `…` | — |
| Runbook | `name` | 60 chars + `…` | — |
| ADR | `title` | 60 chars + `…` | — |

**Selection**:
1. `domains_actifs` = top-N domains of the current project, counted via Neo4j:
   ```cypher
   MATCH (e)-[:BELONGS_TO]->(:Project {project_key: $current})
   MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain)
   WITH d.name AS domain, count(e) AS n
   ORDER BY n DESC LIMIT $top_n
   RETURN domain
   ```
2. `cross_ids` = entities from other projects in these domains, by recency desc:
   ```cypher
   MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain)
   WHERE d.name IN $domains
   MATCH (e)-[:BELONGS_TO]->(p:Project)
   WHERE p.project_key <> $current
   RETURN e.id AS id, labels(e) AS types, p.project_key AS project, e.created_at AS created_at
   ORDER BY e.created_at DESC LIMIT $entries_max
   ```
3. Group `cross_ids` by entity type → 1 PG round-trip per type via `fetch_brief_by_ids(ids)` (max 5 types × 1 query = 5 queries worst case ; typically 1-2 since `entries_max=5`).
4. Re-merge + sort by `created_at` desc, format markdown.

**Empty states**:
- No active domain → section omitted.
- No other projects in these domains → section omitted.
- Neo4j down → log warning + section omitted (old briefing returned intact).
- Env var OFF → section omitted (zero overhead).

**Backward compat**: `_format_session_briefing(ctx, decisions, learnings, cross_entries=None)`. Param added at the end with default `None` — current call unchanged, identical output if `cross_entries` is falsy.

**Latency note**: add 2 Cypher queries + 1-2 PG queries to the briefing path. Soft target: briefing p99 < 500ms. No timeout/SLO enforcement in MVP — measured during rollout J+1, optimize via caching if needed.

### B. `cross_project_resonance.py` script

**Vocabulary**: "resonance" — the algorithm surfaces high-cosine pairs, it does not settle convergence vs drift. A numeric heuristic (regex `\d+\.\d+`) offers an optional hint. The term "drift" no longer appears in file names, branch, env vars, or headings.

**`ResonancePair` dataclass**:

```python
from dataclasses import dataclass
from uuid import UUID

@dataclass(frozen=True)
class ResonancePair:
    a_id: UUID
    b_id: UUID
    a_project: str
    b_project: str
    a_title: str
    b_title: str
    a_created_at: date
    b_created_at: date
    cosine: float
    domain: str  # e.g. "ml"

    @property
    def hint(self) -> str:
        """Heuristic only, never authoritative."""
        nums_a = set(re.findall(r"\d+\.\d+", self.a_title))
        nums_b = set(re.findall(r"\d+\.\d+", self.b_title))
        if nums_a and nums_b and nums_a != nums_b:
            return f"drift candidate (numeric divergence: {nums_a} vs {nums_b})"
        return "convergence likely (no numeric divergence detected)"

    @property
    def dedup_key(self) -> str:
        """SHA256 of canonical pair fingerprint for WET idempotency."""
        lo, hi = sorted([str(self.a_id), str(self.b_id)])
        return hashlib.sha256(f"{lo}|{hi}|{self.domain}".encode()).hexdigest()

    def format_insight(self) -> str:
        """Body for brain_learn.insight in WET mode."""
        return (
            f"Cross-project resonance in domain '{self.domain}' (cosine={self.cosine:.3f}):\n"
            f"- [{self.a_project}] {self.a_title} ({self.a_created_at})\n"
            f"- [{self.b_project}] {self.b_title} ({self.b_created_at})\n"
            f"Hint: {self.hint}"
        )
```

**Algorithm**:
```python
def main(mode: str, domains: list[str] | None, date_str: str | None) -> int:
    if not env_enabled():
        log("cross-project disabled, exiting")
        return 0

    threshold_spec = thresholds.by_name("cross_project_resonance_min")
    if threshold_spec is None:
        log_error("threshold registry missing 'cross_project_resonance_min'")
        return 1
    threshold = threshold_spec.value  # 0.80

    target_domains = domains or sorted(graph_service.ALLOWED_DOMAINS)  # 9

    run_id = await dream_runs_repo.start_run(kind="cross_project_resonance", mode=mode)

    try:
        all_pairs: list[ResonancePair] = []
        for domain in target_domains:
            ids = await graph_service.fetch_decision_ids_in_domain_across_projects(domain)
            if len(ids) < MIN_DECISIONS_PER_DOMAIN:  # 5
                continue
            pairs = await pg_decision.fetch_cross_project_resonance_pairs(
                ids=ids[:MAX_DECISIONS_PER_DOMAIN],  # 200
                threshold=threshold,
                domain=domain,
            )
            all_pairs.extend(pairs)

        all_pairs.sort(key=lambda p: p.cosine, reverse=True)
        all_pairs = all_pairs[:MAX_PAIRS_PER_NIGHT]  # 20

        report_path = build_report_path(date_str)  # artifacts/dream/cross_project_resonance_<UTC-iso-date>.md
        write_markdown_report(report_path, all_pairs, threshold, overwrite=True)

        if mode == "wet":
            if not env_enabled():  # defensive double-check
                log_error("WET blocked: env disabled")
                await dream_runs_repo.end_run(run_id, status="blocked", pair_count=0)
                return 1
            written = 0
            for pair in all_pairs:
                if await learning_repo.exists_by_dedup_key(pair.dedup_key):
                    continue  # idempotent
                await learning_repo.create(
                    topic=f"cross_project_resonance/{pair.domain}",
                    insight=pair.format_insight(),
                    tags=["dream", "cross_project_resonance", pair.domain, "EXCLUDE_FROM_PROMOTE"],
                    project_key="brain-v42",
                    source_kind="cross_project_resonance",
                    dedup_key=pair.dedup_key,
                    dream_run_id=run_id,
                )
                written += 1
            await dream_runs_repo.end_run(run_id, status="completed", pair_count=written)
        else:
            await dream_runs_repo.end_run(run_id, status="completed", pair_count=len(all_pairs))
    except Exception as e:
        await dream_runs_repo.end_run(run_id, status="error", pair_count=0)
        raise

    return 0
```

**PG-side pair computation** (replaces Python O(n²), per J2 critique with the simpler-but-bounded variant):

```python
# In pg_decision.py
async def fetch_cross_project_resonance_pairs(
    self, *, ids: list[UUID], threshold: float, domain: str
) -> list[ResonancePair]:
    """Compute all cross-project pairs above threshold via pgvector <=>.

    Bounded by ids list (capped at MAX_DECISIONS_PER_DOMAIN upstream).
    Excludes intra-project pairs in SQL.
    """
    query = text("""
        SELECT
            a.id AS a_id, b.id AS b_id,
            a.project_key AS a_project, b.project_key AS b_project,
            a.title AS a_title, b.title AS b_title,
            a.created_at::date AS a_created_at, b.created_at::date AS b_created_at,
            (1 - (a.embedding <=> b.embedding))::float AS cosine
        FROM decisions a
        JOIN decisions b ON a.id < b.id  -- avoid self + duplicate pairs
        WHERE a.id = ANY(:ids) AND b.id = ANY(:ids)
          AND a.project_key <> b.project_key
          AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND (1 - (a.embedding <=> b.embedding)) >= :threshold
        ORDER BY cosine DESC
    """)
    rows = await self._execute(query, {"ids": ids, "threshold": threshold})
    return [ResonancePair(domain=domain, **dict(r)) for r in rows]
```

Cost: worst case 200×200/2 = 20k pairs evaluated per domain inside PG, well within pgvector's capability. No embedding payload moves to Python.

**Markdown output**:
```markdown
# Cross-Project Resonance — 2026-05-01

Threshold: 0.80 · Pairs found: 7 · Domains scanned: 9 · Domains with pairs: 4 · Run ID: <uuid>

## Domain: ml (3 pairs)

### Pair 1 — cosine=0.91
- [brain-v42] Decision a3f2e... · "Use Qodo-Embed-1.5B" · 2026-04-15
- [red-shrik] Decision 8c1d4... · "Qodo-Embed for code embedding" · 2026-04-22
- Hint: convergence likely (no numeric divergence detected)

### Pair 2 — cosine=0.83
- [brain-v42] Decision e51fa... · "Cosine 0.92 for dedup" · 2026-04-10
- [red-shrik] Decision 7b9c0... · "Cosine 0.85 for dedup" · 2026-03-28
- Hint: drift candidate (numeric divergence: 0.92 vs 0.85)
```

Empty case (zero pairs):
```markdown
# Cross-Project Resonance — 2026-05-01

Threshold: 0.80 · Pairs found: 0 · Domains scanned: 9 · Domains with pairs: 0 · Run ID: <uuid>

No cross-project resonance pairs above threshold this run.
```

**File policy**: path = `artifacts/dream/cross_project_resonance_<UTC-ISO-date>.md` (e.g. `2026-05-01`). Overwrite if exists (idempotent re-run produces identical content).

### C. Threshold registry

New entry in `src/brain_v42/thresholds.py:REGISTRY`:
```python
ThresholdSpec(
    name="cross_project_resonance_min",
    value=0.80,
    domain="dream",
    rationale="Min cosine to surface decision pair as cross-project resonance candidate",
    calibrated=False,
)
```

Initial 0.80 deliberately below 0.85 (dedup threshold). Recalibrate after 5+ nights of DRY_RUN.

Lookup pattern: `thresholds.by_name("cross_project_resonance_min").value` (returns `ThresholdSpec | None`, defensive `if spec is None: return 1` in the script).

## Configuration

| Var | Default | Effect |
|-----|---------|-------|
| `BRAIN_DREAM_CROSS_PROJECT_ENABLED` | `false` | Master switch. OFF → briefing skip, script exit fast |
| `BRAIN_CROSS_PROJECT_BRIEFING_DOMAINS_TOP_N` | `2` | Top-N active domains surfaced in the briefing |
| `BRAIN_CROSS_PROJECT_BRIEFING_ENTRIES_MAX` | `5` | Briefing entries cap |

Script constants (non env, code-local):
- `MIN_DECISIONS_PER_DOMAIN = 5`
- `MAX_DECISIONS_PER_DOMAIN = 200` (per J2 graceful-degradation cap)
- `MAX_PAIRS_PER_NIGHT = 20`

CLI defaults (`argparse`):
- `--mode` default = `"dry_run"` (explicit)
- `--domains` default = `None` (=all 9)
- `--date` default = `None` (=today UTC)

Threshold cosine: exclusively via `thresholds.by_name("cross_project_resonance_min").value`, no env var.

## Safety & Rollback

1. **Killswitch closed**: MR mergeable without prod risk. Activation = `export BRAIN_DREAM_CROSS_PROJECT_ENABLED=true`.
2. **DRY_RUN by default**: `--mode dry_run` is the argparse default; `--mode wet` requires explicit intent.
3. **Triple WET safeguard**: (a) env var, (b) `--mode wet` flag, (c) inner re-check `env_enabled()` inside WET branch → `exit 1`.
4. **Backward compat briefing**: `cross_entries=None` → output identical to current behavior.
5. **Graceful degradation**: Neo4j down in briefing → section omitted, old briefing intact; PG/embedding failure in the script → `dream_runs.status='error'` + raise (no partial writes).
6. **WET idempotency**: `dedup_key = sha256(sorted(ids) + domain)` prevents duplicates on a same-night re-run.
7. **PROMOTE/CONSOLIDATION insulation**: `EXCLUDE_FROM_PROMOTE` tag + `source_kind="cross_project_resonance"` explicitly filtered by `promote_prepare.fetch_candidates` (modification INCLUDED in the MR).
8. **Rollback**: revert the MR. No migration to reverse.

## Rollout

```
J+0    : MR merged, killswitch closed (no-op in prod)
J+1    : export ENABLED=true locally, verify briefing on 2-3 sessions, measure p99 latency
J+2-5  : run script DRY_RUN manually each night, review .md
J+6+   : if useful signal and 0 aberrant false-positive → consider WET (verify PROMOTE filter active)
J+10+  : if WET stable → add cron nightly (separate follow-up MR)
```

## Test Surface

### Unit tests — briefing

- `test_briefing_skips_cross_section_when_flag_off`
- `test_briefing_skips_cross_section_when_no_active_domains`
- `test_briefing_skips_cross_section_when_no_other_projects_in_domains`
- `test_briefing_includes_top_n_domains_only` (4 domains available, assert exactly 2 come out)
- `test_briefing_top_n_respects_env_var` (override via env)
- `test_briefing_excludes_current_project`
- `test_briefing_orders_entries_by_recency_desc`
- `test_briefing_caps_entries_at_max`
- `test_briefing_format_includes_project_label_type_date_title`
- `test_briefing_truncates_display_field_at_60_chars`
- `test_briefing_graceful_when_neo4j_fails` (mock raises `Neo4jError` → section omitted, old briefing complete)
- `test_briefing_param_optional_backward_compat`

### Unit tests — script

- `test_pair_decisions_excludes_intra_project` (PG SQL contains `a.project_key <> b.project_key`)
- `test_pair_decisions_respects_threshold`
- `test_pair_decisions_caps_at_max_pairs_per_night`
- `test_pair_decisions_skips_domains_below_min_decisions`
- `test_pair_decisions_caps_per_domain_at_max_decisions` (200)
- `test_pair_decisions_skips_null_embeddings` (PG `WHERE embedding IS NOT NULL`)
- `test_dry_run_writes_markdown_report_no_brain_learn`
- `test_wet_mode_writes_brain_learn_per_pair_with_dedup_key`
- `test_wet_mode_idempotent_on_rerun_same_date` (2nd run writes 0 new learnings)
- `test_wet_mode_blocked_when_env_disabled` (inner guard, returns 1)
- `test_dry_run_blocked_when_env_disabled` (outer guard, returns 0)
- `test_report_format_groups_by_domain_with_counts`
- `test_report_format_with_zero_pairs` (empty-case markdown)
- `test_report_includes_threshold_and_metadata_and_run_id`
- `test_report_overwrites_existing_file_same_date`
- `test_resonance_pair_dedup_key_stable_across_id_order` (sorting invariant)
- `test_resonance_pair_format_insight_includes_hint`
- `test_resonance_pair_hint_drift_when_numeric_divergence`
- `test_resonance_pair_hint_convergence_when_no_divergence`
- `test_dream_runs_row_inserted_then_completed`
- `test_dream_runs_row_marked_error_on_exception`

### Unit tests — threshold registry

- `test_cross_project_resonance_threshold_present` (entry exists in REGISTRY, value=0.80, calibrated=False)
- `test_threshold_lookup_via_by_name_returns_value`

### Unit tests — PROMOTE insulation

- `test_promote_excludes_cross_project_resonance_learnings` (fetch_candidates filters tag/source_kind)

### Integration tests

Out of MVP. Reintroduce if/when WET mode opens.

### Coverage

Target: maintain the `60%` CI minimum. Revised v2 estimate: ~350-450 lines of new code (PG SQL + ResonancePair + script + repo methods + PROMOTE filter), ~550-650 lines of tests (~30 unit tests).

### Non-regression

`pytest tests/unit -v` must return 1837/1837 baseline + new tests green at every commit.

## Open Questions Resolved

| Q | Decision |
|---|----------|
| New or existing cross-project topology? | Existing (global Neo4j domains, PG `project_keys` repos) |
| New MCP tools? | No (user constraint) |
| Schema migration? | No (nothing to migrate; adding a `source_kind` column on learnings: to verify during planning, possibly a no-op if already present) |
| Briefing surface: passive (search) or one-shot (session_start)? | One-shot (session_start) |
| Drift surface: sync (warning) or async (nightly script)? | Async (DRY_RUN-able script) |
| Initial threshold value? | 0.80, calibrated=False, re-evaluation after 5+ nights |
| WET allowed by default? | No (DRY_RUN first, WET opt-in after review) |
| Nightly cron by default? | No (manual first, cron in a follow-up MR) |
| Pair compute Python or PG? | **PG** (native pgvector `<=>`, capped by `MAX_DECISIONS_PER_DOMAIN=200`) |
| Terminology everywhere? | "resonance" (branch, script, env vars, headings) — "drift" only as a heuristic hint |
| Threshold registry path/API? | `src/brain_v42/thresholds.py` + `by_name(name).value` (frozen dataclass `ThresholdSpec`) |
| WET idempotency? | `dedup_key = sha256(sorted(ids) + domain)` |
| PROMOTE feedback loop? | Filter `tag=EXCLUDE_FROM_PROMOTE` + `source_kind=cross_project_resonance` in `fetch_candidates` (modification INCLUDED) |

## Follow-ups (out of MVP, captured for tracking)

1. If MVP β works: extend brain_search with a `cross_project=False` param (option α from the original matrix).
2. If MVP β works: inline warning in brain_log_decision (option γ/δ).
3. Nightly cron for the resonance script (separate from this MR).
4. Recalibration of the `cross_project_resonance_min` threshold after 5+ nights of data.
5. Extend resonance to Learnings and ADRs (not just Decisions).
6. Bridge insights generator in SYNTH (initial option B — not selected for the MVP because it requires stable WET).
7. Cache `fetch_active_domains` per (project_key, hour) if briefing latency p99 > 500ms.
8. Embedding model fingerprint column if multiple models coexist one day.

## Changelog

- **v1 (2026-05-01)** — Initial design after brainstorm.
- **v2 (2026-05-01)** — Multi-judge critique applied: terminology consistency (drift→resonance), threshold path corrected (`thresholds.py:by_name`), `ResonancePair` dataclass defined with `format_insight`/`dedup_key`/`hint`, PG-side pair compute via pgvector `<=>` (replaces Python O(n²)), preconditions section added, WET idempotency via dedup_key, feedback-loop insulation via `EXCLUDE_FROM_PROMOTE` tag + `source_kind`, `dream_runs` traceability, CLI defaults explicit, file policy specified, display field mapping table, 2 missing tests added, hint heuristic relocated to dataclass property, `MAX_DECISIONS_PER_DOMAIN=200` cap.
