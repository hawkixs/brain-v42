# Curated roadmap — purge, nightly curator, session surfacing

**Date**: 2026-07-04
**Status**: spec validated (brainstorm session 2026-07-04)
**Related decisions**: `532b9401` (product: red-monitor observes / red-codex works, no merge), `a91dc271` (GitLab webhook decommissioned), `3ed36af1` (id prefix resolution — reused by the write-back)

## 1. Context & diagnosis

Roadmap V2 (March 2026) has a write pipeline that works — 652 features,
3,417 `feature_artifacts`, FeatureLinker + ClusterGuard link every artifact
inserted, last link on the day of this spec — and dead exploitation:

- `brain_get_roadmap` never called; only surfacing = 2 pinned features
  stale (101 d) in the briefing ("In-flight").
- 500 features in `research`: ClusterGuard creates a feature per cluster
  of unmatched artifacts, nobody curates. The ~40 real features
  (deployed/done/building) are drowned out.
- Statuses frozen since June: the StatusEngine maps signal types
  to statuses, and the only signals above `design` were the
  GitLab events (`mr_opened`→building, `mr_merged`/`pipeline_success`→
  deployed). The webhook died on 2026-06-24 (lost secret) and was
  decommissioned on 2026-07-04 — the ladder has no high rungs left.
- Noise: ghost project_keys (`red` 117 features, `refondrre` 36).

## 2. Scoping decisions (validated in brainstorm)

1. **Emergent model assumed + strong curation**: ClusterGuard keeps
   creating; a nightly curator cleans up continuously. (Rejected: hybrid
   candidates/promotion; pure declarative.)
2. **Initial stock**: mechanical purge without LLM, then LLM on the
   remaining ambiguity. (Rejected: all-LLM; full reset.)
3. **Curator operations**: merge duplicates, archive dead ones, status
   transitions, rename titles — all four, proposer-only.
4. **Sessions**: enriched briefing section (replaces In-flight) +
   write-back via a dedicated tool. (Not retained: get_roadmap alone;
   explicit "focus stays king" — the focus does remain de facto the
   source of narrative context, the roadmap becomes the structured view
   of features.)
5. **Curator architecture**: extract/backfill pattern (CLI + killswitch +
   proposals table + review `--apply-ids`). (Rejected: `claude -p` phase;
   mechanical StatusEngine++ alone — empirical evidence against: the
   mechanical version existed and the roadmap rotted anyway.)
6. **Product**: the roadmap lives in brain; red-monitor displays it,
   red-codex will work it. No frontend work in this spec.

## 3. §1 — Data: migration 030

- `features.status`: add `archived` to the CHECK
  (`features_status_check`, currently planned/research/design/building/
  deployed/done).
- `features.merged_into uuid NULL REFERENCES features(id)` — same pattern
  as decisions/learnings. A merge re-points the loser's `feature_artifacts`
  to the survivor THEN marks the loser `merged_into=<survivor>`
  + `status='archived'`. Never a DELETE: the `feature_artifacts` FK is
  ON DELETE CASCADE, deleting would erase the linking history.
  Watch out for the CHECK vs ON DELETE SET NULL gotcha (skill
  `postgres-check-vs-on-delete-set-null`): no CHECK must constrain
  `merged_into`.
- Table `roadmap_curation_proposals` — mirror of
  `ticket_extraction_proposals`:

  ```
  id                bigserial PK
  op                varchar(10) NOT NULL CHECK (op IN ('merge','archive','status','rename'))
  feature_id        uuid NOT NULL REFERENCES features(id) ON DELETE CASCADE
  payload           jsonb NOT NULL      -- merge: {"into": uuid} · status: {"status": "building"}
                                        -- rename: {"name": "…"} · archive: {}
  rationale         text
  status            varchar(10) NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','applied','rejected'))
  created_at        timestamptz NOT NULL DEFAULT now()
  applied_at        timestamptz
  ```

  Index: `(status)`, `(feature_id)`.

## 4. §2 — Mechanical purge: `scripts/roadmap_purge.py`

One-shot, pure SQL, `--dry` by default, per-project report. Rules:

1. `project_key` missing from `project_contexts` AND outside the `red`
   group (reuse `get_keys_by_group`, parity with the codex view) → `archived`.
   The `red` case (117 features) is checked at execution time: if `red` is
   a genuine legacy key, the rule spares it and it gets decided at review.
2. Features with 0 artifacts → `archived`.
3. Features with 1 artifact, no linked artifact created in the last 60 d
   (max(`feature_artifacts.created_at`) — NOT `status_updated_at`), status
   non-terminal (neither deployed, nor done, nor archived) → `archived`.
4. `pinned=true`: never touched by the purge.

Expected output: ~100-150 live features remaining for the curator.
Reversibility: everything is `archived`, a reverse UPDATE suffices.

## 5. §3 — LLM curator: `scripts/roadmap_curate.py`

Exact skeleton of `ticket_extract.py`: `_post_chat` shared from
`domain_backfill` (retry timeouts included, `_exc_str`), strict NVIDIA API
JSON with no tools, `--limit`, `--dry` by default / `--wet`, `--apply-ids`,
`dream_runs` row phase=`roadmap`.

- **Batch per project**: the prompt receives the live features of ONE
  project (non-terminal status, not archived, not merged) + per feature a
  digest of recent artifacts: title, type, date — NOT the full bodies.
  Bounded budget: cap ~30 features/project/night, artifacts cap 10/feature
  (the most recent).
- **Proposed ops**: all 4, JSON array format
  `[{op, feature_id, payload, rationale}]`, strict validation in the style
  of `parse_and_validate` (unknown op rejected, feature_id outside the
  batch rejected, cross-project merge rejected).
- **Guardrails**:
  - `pinned` feature: only the `status` op is proposable;
  - `done`/`archived` features: untouchable;
  - intra-project merge only, `into` must be in the batch;
  - global cap N=40 proposals/night (drop logged if exceeded — no
    silent truncation).
- **Apply (`--apply-ids`)**: single transaction per proposal,
  positive post-conditions checked after each op (re-read the row and
  verify the expected state — learnings F-09 pattern), `Result.mappings()`
  (mypy scripts/ gotcha: tests with `MagicMock(spec=AsyncSession)`).
  `--wet` (propose+apply in the same run) exists but is NEVER used by the
  nightly.

## 6. §4 — Dream step + killswitches

`dream.sh` block identical to the extract step:

- `BRAIN_DREAM_ROADMAP_ENABLED=false` (default) +
  `BRAIN_DREAM_ROADMAP_DRY_RUN=true` — drop-in
  `killswitches.conf` (never in the unit: incident 2026-06-30).
- `timeout 10m`, log `${TIMESTAMP}_roadmap.log`, `SKIP roadmap
  (killswitch…)` logged when closed, `FAIL roadmap (rc=…)` otherwise.
- ROADMAP killswitch line in the session_start briefing (existing
  KillswitchState pattern, tested in test_session_tools).
- Rollout: ≥2 clean dry nights → review the proposals in the morning
  (`--apply-ids` the good ones) → once the acceptance rate is stable and
  high, consider nightly wet for `archive`/`status` only (merge and
  rename stay under review indefinitely).

## 7. §5 — Briefing: "Roadmap" section (replaces "In-flight")

- **Live** features of the project: status ∉ {done, archived} ∧
  merged_into IS NULL, sorted by last artifact activity desc, cap 5.
- Format: `- <name> [<status>] — <N> artifacts, last one <X>d ago`.
- `pinned` at the top of the list (but subject to the same liveness
  criterion).
- The existing "Stale-pinned" section is kept as is (separate alert).
- Graceful degradation: error → section omitted + structlog warning
  (briefing contract §9).

## 8. §6 — Write-back: `brain_feature_update` tool

New MCP tool (surface 41→42):

```
brain_feature_update(feature: str, status: str, project_key: str) -> str
```

- `feature` accepts: exact name → id prefix (≥8 hex,
  `resolve_entity_id` reused, FeatureService/repo passthrough to create
  on the resolve_id_prefix pattern) → unique ILIKE on the name.
  Ambiguity → error listing the candidates (id + name). No match →
  explicit error.
- `status`: CHECK values only (including `archived` — a session can
  archive a bogus feature by hand).
- Side-effects: `status_updated_at=now()`, `pinned=true` (same behavior
  as the current update_feature_statuses path).
- The old `brain_update_project_focus(feature_status=…)` path stays
  functional (backward compat) but the docs point to the new tool.
- Project CLAUDE.md: add the instruction "feature shipped →
  `brain_feature_update(name, 'deployed'|'done')`".

## 9. §7 — Dream metrics sidecar up to date (consumed by red-monitor)

Observed on 2026-07-04 on the live /metrics, two defects in
`collector_dream.py`:

1. **Aggregate polluted by re-runs**: the query takes ALL the rows of
   `dream_runs` for the latest `run_date` (L46-53). A run replayed on the
   same day (e.g. extract fail 06:13 then done 10:58) gives
   `phases_fail:1, status:"partial"` even though each phase ended done.
   Fix: `DISTINCT ON (phase) … ORDER BY phase, id DESC` — last row per
   phase; `phases_ok`/`phases_fail`/`status` computed on the deduplicated
   set.
2. **No dry/wet flag per phase**: `dream_runs.phase_dry_run` exists but
   is not exposed. Add `"dry_run": bool` to each `phases` entry —
   red-monitor can badge wet/dry. A phase absent for the day = closed
   killswitch (SKIP does not create a row): the frontend can infer
   "off" by diffing against the phases seen in `history`.

Constraints:

- **Additive contract only**: /metrics is consumed by red-monitor — new
  keys OK, no rename/removal (red-triad contracts reflex, synthesis
  `f32168ae`).
- **`brain-metrics` restart mandatory** after deployment: the sidecar is
  in no deploy loop (learning `f13144b3`).
- The CLI phases (extract, roadmap) legitimately have `cost:0`.
  **Amended on 2026-08-05**: `model:null` no longer holds for `roadmap`,
  which now records the model actually used. Leaving this column null
  hid ten nights served by the fallback model after the primary's EOL
  (ticket `911bb6f5`). `extract` stays at `model:null`.

## 10. §8 — GitLab signal successor

- **v1 (included)**: the curator reads the CONTENT of artifacts — a
  decision "shipped X", a runbook "deploy X", a plan status=done →
  proposes `status: deployed`/`done`. Richer than the old mapping by
  event type.
- **v2 (deferred, out of scope)**: nightly `gitlab_poll` step — PULL
  from the GitLab API (commits/MRs/pipelines per project, read-only
  token) ingested into `gitlab_events`. Zero exposed port, zero webhook
  secret: the pull fixes what killed the push. To be specified
  separately if the need for commit↔feature correlation returns.

## 11. Non-goals & risks

- **Non-goals v1**: ClusterGuard/FeatureLinker unchanged (the write path
  works); no frontend work (red-monitor reads the same data, better, via
  its existing contracts; red-codex later); no gitlab_poll.
- **Watchk risk**: conceptual overlap (Watchk = manual declarative
  features, project 6). Position: brain roadmap = emergent/auto-linked,
  Watchk = declarative tracking. To be re-settled if the duplication
  becomes painful — out of scope here.
- **Curator quality risk**: the roadmap depends on nightly judgment.
  Mitigated by: proposer-only + review, hard guardrails, caps, and the
  domain_backfill precedent (36/36 accepted).
- **NVIDIA cost**: ~8 live projects × 1 call/night, variable queue
  latency (retry in place). Bounded by the caps.

## 12. Success criteria

1. Purge: live features ≤ 150, zero unarbitrated ghost project_key.
2. After 1 week of curation: `deployed`/`done` statuses reflect the
   actual deliveries of the week (verifiable against the focuses).
3. The briefing shows live features with activity < 7 d (no more
   "planned — updated 101d ago").
4. At least one successful `brain_feature_update` per delivery session.
5. Sidecar: a night with a phase re-run displays `done` (not
   `partial`), and each phase carries its `dry_run` flag.
6. Usual gates: pytest green, ruff, mypy src/, coverage ≥ 60%.

## 13. TDD breakdown (indicative, for the plan)

1. Migration 030 + schema tests (test_schema_indexes_027 pattern).
2. `roadmap_purge.py` + rule tests (mocked session, no real DB).
3. `roadmap_curate.py` propose + parse_and_validate + guardrails.
4. Apply + post-conditions.
5. dream.sh step + test_dream_sh_roadmap (test_dream_sh_extract pattern).
6. Briefing section (test_session_tools).
7. `brain_feature_update` + resolver (tool tests, id prefix pattern).
8. collector_dream: DISTINCT ON dedup + dry_run per phase
   (tests/unit/metrics/, additive contract) + brain-metrics restart on
   deploy.
9. Docs: CLAUDE.md instruction, MCP_TOOLS, .env killswitches example.
