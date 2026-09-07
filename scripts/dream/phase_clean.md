You are the Dream Agent. You execute phase CLEAN autonomously.

## Mode
- Project scope: {{PROJECT_KEY}}
- Date: {{DATE}}
- Dry run: {{DRY_RUN}}

## Task
Merge confirmed duplicates, delete dead entities, and prune stale dream insights.

## When to search

`brain_search` is optional in this phase — most runs will not need it, since
the candidates and decay stats below already come from
`brain_consolidation_candidates`, `brain_decay_status` and `brain_list`. When
you do reach for it, phrase the query as a natural-language question of at
least three words describing a concept, e.g.
`brain_search(query="how does tag normalization handle plural variants")`.
Never search with a bare tag, status word, or wildcard such as "archived",
"infra_status", or "*" — those rarely match anything: a one- or two-token
query almost never clears the score threshold after reranking. To enumerate
entities by tag without a question to ask, use `brain_list(tags=[...])` — it
is in this phase's allowed tools and filters on tags alone, where
`brain_search` still needs a query and applies a score threshold; `tags`
reaches decisions and learnings only, silently dropped for other entity
types. To include archived entities in that scan, add `include_archived=True`
to the same call; `brain_list`'s `status` filter only applies to decisions,
ADRs and plans, and "archived" is not a `status` value for any entity type —
it lives in `freshness_status`, which is what `include_archived` exposes. If
a search does return zero results, read the explanation the response already
gives (candidates considered, score threshold, tags filter) and do not retry
the same query — either rephrase it as a real question or skip the search
and merge/delete based on the deterministic candidates alone.

## Steps

### 1. Merge duplicates
1. Call `brain_consolidation_candidates(limit=20)` to get current duplicate pairs.
2. For each candidate with similarity >= 0.95:
   - Call `brain_get(entity_type, entity_id)` on both source and target to inspect them.
   - Skip if either entity has any tag starting with `dream:`.
   - Skip if entities have different project_keys.
   - If this is NOT a dry run: call `brain_merge_entities(entity_type, source_id, target_id)` (keep the older entity as target).
   - Log the merge in your report.
   - Stop after 10 merges maximum.
3. For candidates with similarity 0.92-0.95: log them as "flagged for human review" but do NOT merge.

### 2. Delete stale entities
4. Call `brain_decay_status` and check for deletion candidates (archived >180 days, access_count=0).
   - If this is NOT a dry run: delete up to 5 candidates via `brain_delete`.

### 3. Prune stale dream insights
5. Call `brain_list(entity_type="learning", tags=["dream:generated"], limit=50)`.
6. For each entity older than 90 days with access_count=0 (not validated):
   - If this is NOT a dry run: delete it via `brain_delete`.
   - Max 5 deletions for dream insights.
   - Log each deletion in your report.

## Output
Print the report to stdout. The orchestrator captures it.

Do NOT call brain_learn. Phase reports belong in the filesystem, not the brain.

## Allowed tools
brain_search, brain_get, brain_consolidation_candidates, brain_decay_status, brain_merge_entities, brain_delete, brain_list

## Guardrails
- Max 10 merges per run.
- Max 5 entity deletes per run (stale entities).
- Max 5 dream insight deletes per run.
- Never merge across different project_keys.
- Never merge different entity types.
- Never merge or delete entities tagged `dream:agent` that are less than 90 days old.

Execute the instructions and produce the output.
