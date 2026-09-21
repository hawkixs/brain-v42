# What does a feature's embedding encode?

**Status: open question, gating the codestral-embed provider switch.**
Written 2026-09-21 after a review of the switch's reindex path found that reindexing
`features` changes how the roadmap links its artifacts.

**Corrected the same day** after an independent frontier review
(`2026-09-21-feature-embedding-definition-review-codex-astra.md`) refuted two load-bearing
claims of the first draft. Both refutations were verified against the code and re-measured.
The corrections are marked inline; the first draft said "blocking" where the evidence supports
"gating", and asserted a flood the resolver's own thresholds rule out.

## The column

`features.embedding` is `vector(1536)`, like the eight other vector tables. Unlike them it
serves **no search**: `brain_search` never queries it, and `access_log_daily` holds no row of
type `feature`. It drives exactly one thing — `cluster_guard.resolve()` deciding, for each
incoming signal, whether to LINK it to an existing feature or CREATE a new one, at
`COSINE_LINK = 0.70`.

So a stale vector here does not degrade a ranking. It changes a decision, and the decision it
changes is the one whose runaway produced the pseudo-feature flood shut off on 2026-08-03 —
479 of the 920 rows are archived, which is that episode's scar.

## Four writers, four texts

Measured on the repository at `825e46da`:

| Writer | What it embeds |
| --- | --- |
| `cluster_guard._create_feature` | the **caller's** embedding, computed from the source artifact's text |
| `cluster_guard._merge_into` | `embed(old_description + "\n\n---\n" + text)` |
| `feature_creation_service.create` | `embed(name + "\n\n" + description)` |
| `scripts/regen_embeddings.py` (new) | `embed(description)` |

No column records which of the four produced a given row's vector. This is the same shape as
the provider problem one level down: one column, several producers, no provenance.

## Why no reindex can be faithful

The dominant writer stores the source artifact's text as a vector and **does not store that
text**. `description` holds something else — measured on the 250 features linked to a plan, it
averages 179 characters, while a plan's `title + summary` averages 64.

So there is no text on the row from which the current vectors can be recomputed. Any reindex
picks a new definition. `description` is **one** reproducible candidate — `name` is a column
too, so `name + description`, which `feature_creation_service` already embeds, is equally
reproducible. The first draft called `description` the only candidate; that was wrong.
Whichever is chosen, choosing one is what makes the column verifiable by
`check_embedding_model_drift.py` instead of unverifiable forever.

**The consequence is not a defect of the reindex. It is structural: reindexing this column at
all changes linking behaviour, however well it is done.** Not reindexing is not an escape
either — after a provider switch the old vectors are noise, and cosine against the new model's
signals sits near zero, which links nothing at all.

## What the change costs, measured

Measured 2026-09-21 on the 239 plans already linked to a feature, computing
`cosine(plan.embedding, embed(feature.description))` with the model currently in production, so
the provider is held constant and only the text definition varies:

```
n=239   min 0.512   p25 0.669   median 0.751   p75 0.843   max 1.000

>= 0.70    link directly                158  (66 %)
0.50-0.70  grey zone, reranker decides    81  (34 %)
<  0.50    create directly                 0  ( 0 %)
reranker unavailable, < FALLBACK_LINK 0.65 -> create :  46  (19 %)
```

**The first draft read the 34 % as "a third of those plans would mint a feature". That is
wrong**, and the resolver's own thresholds say so: below `COSINE_LINK` it does not create, it
enters a grey zone at `COSINE_GREY_LOW = 0.50` and consults the reranker, which links at 0.75
and merges at 0.50. Not one of the 239 pairs falls below 0.50, so none reaches the direct
creation path.

What the number actually says: a third of these plans would move from a deterministic link to a
reranker-mediated decision. The exposure is therefore **conditional on the reranker**, which
this project already lists as intermittently returning 503. When it is unavailable,
`_fallback_cosine_only` links at 0.65 and creates below it — 46 of 239, 19 %.

Re-running plan indexing after such a reindex is not optional as the sequence stands: the switch
marks all 208 plans stale so they re-embed, and `signal_type="plan"` is in `CREATING_SIGNALS`,
so link-only mode never skips it.

That last fact also explains an anomaly in the project's own blockers: the tap is recorded as
"closed since 2026-08-03, zero creation since", yet 44 features have been created since. The
record is not stale — `plan` simply bypasses link-only by design.

## The options, and what each leaves behind

**A — Align the writers on `embed(description)`.** The column gains a single definition and
becomes verifiable for good. It does **not** restore the 34 %: a plan's signal stays
`embed(title + summary)` while the feature becomes `embed(description)`, two different texts in
one space. It needs a companion decision, retuning `COSINE_LINK` against the distribution
above. Cost: code, review, release, plus a tuning measurement of its own.

**B — Take `plan` out of `CREATING_SIGNALS` for the switch window.** One constant: a
below-threshold plan skips instead of minting. **It does not cost the 81 plans their existing
link** — the first draft said it did. `_link_plan_to_feature` is an insert with
`on_conflict_do_nothing`, so a skipped resolution deletes nothing and the link survives; what it
requires is handling the `skipped` return instead of asserting a feature exists. It still fixes
nothing structural: on the first feature write after the window the column starts re-mixing,
because the runtime writers are unchanged. Containment, not repair — but cheaper than the first
draft claimed.

**C — Do neither yet.** Land what nobody disputes (the snippet composition, the drift check's
median verdict, the runbook's missing "stop the writers" step) and treat this column as its own
piece of work. Costs only the trial's start date, which the project's own focus calls not
urgent.

**D — Record the source text.** Add the text the vector was computed from to the row. It buys
provenance, and it is the largest change. It does **not** recover the historical inputs already
lost, and it does not by itself say whether the chosen text matches well — provenance is not
quality. It also needs a versioned model-and-composition contract to mean anything.

**E — Separate the embedding refresh from the feature resolution.** Re-embedding an unchanged
plan does not have to reopen its feature assignment. The document's first draft treated the
plan indexer's unconditional call to `cluster_guard.resolve()` as a constraint of the operation;
it is an implementation choice. A provider-only rebuild could preserve existing relationships
and perform no creation or merge at all, leaving resolution for new or genuinely changed
signals. This option was raised by the independent review and is the one it would take, coupled
with aligning the writers on a versioned row-derived recipe — `name + description` being a
reasonable starting candidate.

## The question

Should a feature's vector be **verifiable** — reconstructible from its own row, which is what a
drift check requires — or **faithful** to the space of the signals that interrogate it, which is
what linking requires?

The independent review argues this is largely a manufactured dilemma: two different texts
embedded by one model do not occupy incompatible spaces, and preserving the first signal's
vector never established that it represents the feature well in the first place. What is real,
it holds, is a narrower defect — the plan path embeds `title + summary` but passes only `title`
as the feature's text, which is an inconsistent input contract rather than an impossibility.

That reading is the stronger one, and it moves the decision: the question is not which side of a
trade-off to take, but which row-derived recipe to standardise on, and how to validate the whole
resolver — candidate eligibility, links, merges and creations, against judged matches and hard
negatives — before a cutover, rather than retuning a threshold on historical positives alone.
