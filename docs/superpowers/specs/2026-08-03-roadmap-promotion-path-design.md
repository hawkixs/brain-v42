# Link-only mode for knowledge signals — design

**Date**: 2026-08-03
**Status**: validated, ready for an implementation plan
**Learning source**: `8d2b322e-5c0a-409d-96fe-9dca8a784aa9`
**Related ticket**: `d9a968f7-03bc-4ed5-b04b-2ba7a561abd0` (raw SQL path, out of scope)

## 1. Problem

The `brain-v42` roadmap carries 171 features, 71 of them with active `research` status. Of
these 71, 64 exactly duplicate the name of an existing knowledge artifact: 48 learning
topics, 16 decision titles, zero overlap. The remaining 7 are promoted commit
messages.

This is not a historical stock but an active flow. Logging a decision makes a
pseudo-feature carrying its title appear within the second.

The exact path:

```
brain_log_decision
  → DecisionService.create
  → link_artifact_if_enabled          (services/decision_service.py:135)
  → FeatureLinker.link_artifact
  → FeatureLinker._do_link_via_guard  (services/feature_linker.py:92)
  → ClusterGuard.resolve
  → _create_feature                   (services/cluster_guard.py:105)
```

`ClusterGuard.resolve()` exposes no "link or do nothing" mode. Without a
similar candidate, it creates, naming the feature after the artifact's title. Same path for
learnings, snippets, runbooks and ADRs.

This is not an implementation bug: it is the "Feature Auto-Tracking / Roadmap" feature
working as designed. The flaw is conceptual — it does not distinguish a **knowledge**
artifact (an insight, which is not a deliverable) from a **work** signal (a plan,
a GitLab event, which is one).

The boundary is already drawn elsewhere in the code. `StatusEngine.SIGNAL_STATUS_MAP` splits
exactly the same two families:

| Signal | Target status | Family |
|---|---|---|
| `learning`, `decision`, `snippet` | `research` | knowledge |
| `runbook`, `adr` | `design` | knowledge |
| `plan` | `design` | work |
| `mr_opened` | `building` | work |
| `mr_merged`, `pipeline_success` | `deployed` | work |
| `push`, `pipeline_failure` | (none) | work |

## 2. Scoping decisions

Four choices validated with the operator before design.

**Behavior adopted: link on match, never create.** Linking keeps its value when
it lands right — it is what feeds the "3 artifacts, last one 1d ago" in the roadmap
briefing. We are not cutting the linking, we are cutting the creation.

**Scope: all five knowledge types without distinction.** The option of letting
`runbook` and `adr` create features, on the grounds that they describe a deliverable, was
discarded: two regimes to maintain and document for a marginal gain.

**Removal of `merged` too.** In the gray zone, the old behavior concatenated the
artifact's text to the feature's description and then re-embedded it
(`cluster_guard.py:249-277`). Applied to knowledge, this drifts a feature's identity:
concatenation after concatenation, its embedding moves away from its own subject and
attracts anything. `link-only` therefore strictly means "link if confident, otherwise nothing".

**Cleanup of the 64 existing ones: out of scope.** Fix the tap, measure the
drying-up, empty the bathtub afterward. Purging in the same delivery would make success
unverifiable: impossible to tell "the fix works" apart from "the purge masked the
problem". Any new pseudo-feature appearing after this delivery is a direct
signal that the fix is incomplete.

## 3. Blast radius

`impact({target: "resolve", direction: "upstream"})` returns **CRITICAL**: 695 symbols,
79 processes, 20 modules. The figure describes the symbol's centrality, not the size of the
change — `direct: 1`, and the 625 depth-2 symbols are the transitive closure of
the MCP tool surface, where every `brain_*` goes through the services layer.

Actual callers of `ClusterGuard.resolve()`:

| Caller | Signals emitted | Effect of the change |
|---|---|---|
| `FeatureLinker._do_link_via_guard` | knowledge (5 types) | **target of the fix** |
| `plan_indexer.py:362` | `plan` | none |
| `gitlab_ingestor.py:100` | `mr_opened`, `mr_merged`, `push`, `pipeline_success` | none |
| `gitlab_ingestor.py:100` | `mr_closed` | **fixed, see §4.1** |

Flows to watch for non-regression: `Brain_reindex_plans` and the GitLab ingestion must
keep their creation capability identical, with the sole exception of `mr_closed`
documented in §4.1.

`pipeline_failure` never reaches `resolve()`: `gitlab_ingestor` exits upstream with
`{"status": "skipped_pipeline_failure"}`. Its presence in the allowlist is therefore inert,
and kept only so the list stays readable as "all work signals".

## 4. Design

### 4.1 Creation allowlist

An explicit constant in `src/brain_v42/services/cluster_guard.py`:

```python
CREATING_SIGNALS = frozenset({
    "plan", "mr_opened", "mr_merged", "push",
    "pipeline_success", "pipeline_failure",
})
```

**`mr_closed` is deliberately outside the allowlist**, and it is the only work signal whose
behavior changes. It does reach `resolve()` and, until now, created a `planned` feature
for lack of a candidate. But the `gitlab_ingestor` docstring already documented this signal as
`(linking only)`: it was the implementation that contradicted its own contract. A closed merge
request is not a deliverable, and a roadmap feature named after its title falls
exactly under the pollution this project removes. A deliberate correction, settled by
the operator on 2026-08-03, not an unnoticed side effect.

**Allowlist, not denylist, by fail-closed choice.** An unknown `signal_type` — a new
`brain_*` tool, an artifact type added later — falls into link-only and creates nothing. The
failure mode fixed here is over-creation; the default leans the opposite way. Deliberate
creation keeps its own dedicated, fail-closed path, `brain_feature_create`
(decision `1e9b1929-66e0-4c2a-b3fc-aef11d759355`).

### 4.2 Resolution rule

Outside `CREATING_SIGNALS`, the branches that link are kept, all the others
return `(None, "skipped")`.

| Situation | Work signal | Knowledge signal |
|---|---|---|
| No candidate | `created` | `skipped` |
| Cosine ≥ `COSINE_LINK` (0.70) | `linked` + status promotion | `linked` + status promotion |
| Gray zone, reranker ≥ `RERANKER_LINK` (0.75) | `linked` | `linked` |
| Gray zone, reranker ∈ [0.50, 0.75) | `merged` | `skipped` |
| Gray zone, reranker < `RERANKER_MERGE` (0.50) | `created` | `skipped` |
| Reranker unavailable, cosine ≥ `FALLBACK_LINK` (0.65) | `linked` | `linked` |
| Reranker unavailable, cosine < 0.65 | `created` | `skipped` |
| Cosine < `COSINE_GREY_LOW` (0.50) | `created` | `skipped` |

No threshold is changed.

**Status promotion is kept on `linked`.** A learning that matches an existing feature
at 0.70+ moves it `planned` → `research`. That is a legitimate signal on a
feature that already exists, and removing it would drain the linking of its point.

### 4.3 Return contract

```python
async def resolve(...) -> tuple[object | None, Literal["linked", "merged", "created", "skipped"]]
```

`skipped` is the only value carrying `None`.

`FeatureLinker._do_link_via_guard` tests `if feature is None: return 0` **before** the insert
into `feature_artifacts`; without this guard, accessing `feature.id` would raise on `None`.

`plan_indexer` only ever emits `plan` and therefore never receives `skipped`: the `| None` is
handled there with an explicit assertion, never `# type: ignore`.

`gitlab_ingestor` **can** receive `skipped`, on `mr_closed` (§4.1). It therefore cannot
settle for an assertion: it stores the event with `feature_id=None` and does not insert
a row into `feature_artifacts`. A GitLab event still gets recorded even without a feature to
attach it to.

### 4.4 Observability

A structured `cluster_guard.skipped` log carrying `signal_type`, `project_key` and the best
score obtained.

This is not a comfort feature. The strategy adopted in §2 defers the purge until the
drying-up is verified, and that is only verifiable if the drying-up leaves a trace. This log
answers "how many creations did the fix avoid" without a SQL query, and above all
detects a residual `created` on a knowledge type — the only sign of an
incomplete fix.

## 5. Tests

Strict TDD, RED before any implementation.

**`ClusterGuard.resolve()`**

1. `signal_type="learning"`, no candidate → `(None, "skipped")` and no row inserted
   into `features`
2. `signal_type="learning"`, candidate at 0.85 → `linked`, status promoted `planned` → `research`
3. `signal_type="learning"`, gray zone reranker 0.60 → `skipped`, candidate feature's
   description **unchanged** — this is the test that proves the removal of `merged`
4. `signal_type="plan"`, no candidate → `created` — non-regression of work signals
5. `signal_type="type_inconnu"` → `skipped` — proves the allowlist's fail-closed

**`FeatureLinker`**

6. `resolve()` returns `(None, "skipped")` → 0 links returned, no row in
   `feature_artifacts`

**Integration** — `tests/integration/test_cluster_guard_link_only.py`

Delivered in a more targeted form than the original statement. The unit tests mock the
session: they prove the branching, not that the missing `INSERT` is really missing. The
two integration tests therefore exercise `ClusterGuard.resolve()` against a real
PostgreSQL+pgvector and count the `features` rows afterward.

7. Knowledge signal on an empty project → `(None, "skipped")` and **zero rows** in
   `features`
8. `plan` signal on an empty project → `created` and **exactly one row** — keeps the other
   half of the contract, link-only must not become "never creates anything again"

Neither GPU nor reranker required: on an empty project `_find_candidates` returns nothing,
and resolution goes straight to the create-or-skip decision without calling these services.

These two tests were verified by mutation — adding `decision` to `CREATING_SIGNALS` makes
the first one fail on `assert 'created' == 'skipped'`. An integration test that cannot
fail proves nothing, and this batch already contained one (§5, note on the reranker
fallback test's assertion).

## 6. Out of scope

- **The 64 existing pseudo-features** — separate ticket, after the drying-up is measured (§2).
- **The raw SQL path `FeatureLinker._do_link`** — ticket
  `d9a968f7-03bc-4ed5-b04b-2ba7a561abd0`. It creates no feature and therefore does not
  contribute to the pollution, but keeps a divergent semantics: links to *all* features
  above 0.70 instead of the best one, ignores the reranker, promotes no status.
  Unifying it in the same delivery would make the drying-up unattributable.
- **The `deployed` status of the "Feature Auto-Tracking" roadmap feature**, which would
  deserve review after this delivery.
- **The thresholds** `COSINE_LINK`, `COSINE_GREY_LOW`, `RERANKER_LINK`, `RERANKER_MERGE`,
  `FALLBACK_LINK`, `_DEFAULT_THRESHOLD` — none of them is touched.

## 7. Success criterion

After delivery, logging a decision or a learning on `brain-v42` creates no feature,
and linking to a sufficiently close existing feature keeps working and
promoting its status. The `plan`, `mr_opened`, `mr_merged`, `push` and
`pipeline_success` signals create features identically; `mr_closed` no longer creates,
by deliberate correction (§4.1).
