# Plan index Codestral recovery

## Purpose

Use `scripts/refresh_plan_embeddings.py` only after the plan inventory repair
has completed and plan-index writers are quiescent.  It never scans a source
tree, creates roadmap features, removes plans, chunks, or feature links, or
claims that a source file was freshly indexed.  It changes only the embedding
columns of the selected stored parent/chunk cohort.

The default command is read-only.  It opens no embedding-provider client and
makes no database write:

```bash
python scripts/refresh_plan_embeddings.py --project red-games
```

Without `--postgres-url`, the database comes from the effective environment.
An explicit URL overrides that database selection, including its host; provider
configuration still comes from the environment. Keep credentials out of command
history and process arguments by using the configured environment for production.

Read the reported parent and chunk counts from that exact run.  Do not copy a
historical count or a projection into a write command.  The optional project
filter includes that project's parents and all of their chunks, including
archived parents; archived rows can be the last stored copy of a plan.

## Preconditions

Before an apply:

1. Pause plan indexers and any writer that can change `indexed_plans` or
   `indexed_plan_chunks` for the duration of the operation.
2. Verify the configured database name and embedding model from the same
   environment that will run the command.
3. Choose a new, absolute recovery path in a private non-symlinked directory.
   The command exclusively creates it at mode `0600`; a collision is a refusal,
   never an overwrite.
4. Use the counts just read and the exact database/model identities.  A changed
   cohort, source field, project assignment, or original vector is refused.

The recovery JSON is fsynced before the first update.  It contains the original
parent/chunk source state, IDs, project relationships, and vectors needed to
investigate or reverse a failed operational attempt.  Treat it as private
operational data and retain it according to the incident record.

## Apply

The default batch size is two. It fits the request budget in the
[official Codestral cookbook](https://docs.mistral.ai/resources/cookbooks/mistral-embeddings-code_embedding)
when each input fits its 8k input window; the CLI accepts only batch sizes
from 1 through 100. Larger batches require shorter inputs. Do not change the
canonical text or its truncation to work around a provider refusal.

```bash
python scripts/refresh_plan_embeddings.py \
  --apply \
  --project red-games \
  --recovery-file /secure/brain-recovery/plan-vectors-red-games.json \
  --expected-plans <read-count> \
  --expected-chunks <read-count> \
  --expected-database <current_database> \
  --expected-model <configured-model> \
  --batch-size 2
```

The provider is called before the bounded write transaction.  The transaction
locks both vector tables, rereads the exact cohort, writes the private recovery
snapshot, then updates only `embedding`.  The existing parent timestamp trigger
advances `updated_at`; `indexed_at`, freshness fields, IDs, plan text, chunk
text, project keys, and feature links are not written by this command.

Any refusal or provider/database failure exits nonzero and reports only a safe
failure category.  It never prints a DSN, provider response body, or plan body.
After a successful operation, run the independent drift check for both `plan`
and `plan_chunk` and retain the recovery JSON with the measured evidence.

A cleanup failure can occur after the commit: the command then prints its
successful update counts followed by `plan vector refresh cleanup failed` and
returns nonzero. That exit code does not mean the database is unchanged. Inspect
the update report, recovery file and actual vectors before deciding to resume;
never replay or restore solely because the exit code is nonzero.

## Fill missing or changed files before refreshing stored vectors

This is a separate phase, after the scan-path and ownership repair in the
[inventory runbook](2026-09-22-plan-index-inventory-and-repair.md). Keep writers
quiescent. Before the first repair, save a complete, private database snapshot
of the affected project contexts, all indexed parents and chunks (including
their texts and vectors), and all plan feature edges. The inventory recovery
file contains only the fields needed for ownership repair; the vector recovery
file is taken after this indexing phase. Neither alone recovers old derived
chunks replaced while indexing a changed file.

Use the reviewed release environment and its effective database/provider
configuration. Invoke the indexer explicitly with feature linking disabled:

```python
import asyncio
import json

from brain_v42.config import get_settings
from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.services.embedding_factory import build_embedding_service
from brain_v42.services.plan_indexer import PlanIndexer


async def fill_missing_plans():
    provider = build_embedding_service(get_settings())
    try:
        indexer = PlanIndexer(
            get_session_factory(), provider, cluster_guard=None, link_features=False
        )
        results = await indexer.index_all_projects()
        print(json.dumps(results, sort_keys=True))
        if any(stats.get("errors", 0) for stats in results.values()):
            raise SystemExit("plan indexing incomplete; stop and inspect the private log")
    finally:
        try:
            await provider.close()
        finally:
            await dispose_engine()


asyncio.run(fill_missing_plans())
```

Do not call `brain_reindex_plans` for this incident: it also calls
`dedupe_plans`, which deletes duplicate parents and cascades to their chunks.
The maintenance option above suppresses feature resolution, creation, merging
and linking for both new and changed plans. A normal file upsert can replace
derived chunks; it does not delete archived last copies. Indexing commits per
file, so a failed sweep is not an all-or-nothing transaction: retain its report
and inspect the completed files before resuming.

Verify file/project/hash coverage and unchanged archived parent/chunk identities
and text after this phase. Remeasure the complete stored cohort, then run the
vector refresh above, including archives. Check feature edges and parent/chunk
ownership again. Keep periodic refresh disabled during recovery. A server
restart still runs one initial indexing pass, so complete these checks before
restarting the normal writer.
