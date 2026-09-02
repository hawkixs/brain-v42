# Migration 050 — applying the focus history, arming its guard, recovering a focus

§5.6 of `docs/design/refonte-projets-sessions/PLAN-phase-0-4.md` (M-D). Written
2026-09-02, before the apply. Production was measured at `049` that day, with no
`project_focus_history` table; the repository head is `051`.

This runbook exists for one reason: the apply has a **derogatory order**, and the
one step that can hurt is the one that looks harmless.

## What 050 adds, in one paragraph

`brain_set_project_context` rewrites `current_focus` with no CAS — including to
NULL when the argument is omitted. The channel was never silent (032 bumps the
revision, 040 dates the change); it was UNRECOVERABLE, because nothing kept the
prose it replaced. 050 adds `project_focus_history`, append-only, one row per
persisted revision, written by all seven focus writers inside the transaction
that writes the focus. Plus a DEFERRED CONSTRAINT TRIGGER on `project_contexts`
that, at COMMIT, requires that row to exist — **shipped disabled**.

## The one dangerous step, first

**Never arm the trigger before restarting the MCP process.**

Between `alembic upgrade` and the restart, the live process still runs pre-050
code. That code writes no history row. An armed trigger would therefore abort, at
COMMIT, **every `brain_session_end` that applies a focus** — fail-closed, session
left open, with no practicable killswitch and no downgrade that does not destroy
the audit trail. The window is short and the failure is total.

Everything else in this runbook is reversible. This is the step to read twice.

## Apply

Expected on the day of writing: 59 contexts, 10 of them with a NULL focus.
Re-measure rather than copy — that is the point of the checks.

```bash
# 0. Dump BEFORE. Same gesture as 049.
docker exec brain_v42_postgres pg_dump -U brain -d brain -Fc -f /tmp/pre050.dump

# 1. Apply, and PROVE it before anything restarts.
.venv/bin/python -m alembic upgrade head

docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select version_num from alembic_version;"
# → 051 (050 and 051 travel in one PR)

docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select (select count(*) from project_contexts) as contexts,
          (select count(*) from project_focus_history
            where source = 'migration_seed') as seeded;"
# → the two numbers MUST be equal. The seed covers every context, NULL focus
#   included: a context whose revision carries no history row would abort at
#   COMMIT on its first focus write once the trigger is armed.

docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select tgenabled from pg_trigger
    where tgname = 'project_contexts_focus_history_required';"
# → D. If this reads O at this point, STOP and disable it before step 2.

# 2. Restart the MCP. Required, and required HERE.
systemctl --user restart brain-mcp-http

# 3. Only now, arm the guard. DATE this gesture — see "the switch costs something".
docker exec brain_v42_postgres psql -U brain -d brain -c \
  "ALTER TABLE project_contexts
     ENABLE TRIGGER project_contexts_focus_history_required;"

# 4. Prove the armed guard on a throwaway project, never on a real one.
#    A focus write through brain_update_project_focus must succeed; a bare
#    UPDATE must abort at COMMIT (see "restoring a focus" below for why).

# 5. Re-mint the DR contract — v8. AFTER arming, never before.
```

## The two attestation reds, and when they close

Between the apply and step 5, two checks in
`tests/integration/db/test_fresh_head_is_the_yardstick.py` fail, **by design**:

- `runtime_trigger_mismatches: 1` — the v7 asset requires every runtime trigger
  in `tgenabled = 'O'`, and this one is `D` until step 3;
- `table_set` and the index counts — the asset was minted at head 049 and does
  not know `project_focus_history`.

Do not regenerate the asset to make them green before the apply: an asset minted
early describes a database that does not exist. They close at step 5.

A third check in the same file may fail for an unrelated reason —
`brain_session_checkpoints` CHECKs declared in `tables.py` — which belongs to
051, not here.

## The switch costs something

While the trigger is disabled, the `ops/recovery` attestation is red. That is
deliberate: it is what keeps "disabled" from becoming a state nobody notices.
Reusing the switch in an emergency is legitimate; doing so without dating it is
not. The integration schema guard
(`tests/integration/schema_fingerprint.py`) accepts this ONE trigger in `D` or
`O` and nothing else — `R` or `A` on the same name still refuses, as does
disabling any other trigger.

## Rollback

```bash
.venv/bin/python -m alembic -x allow_focus_history_downgrade=yes downgrade 049
```

Without the opt-in the downgrade **refuses**, and names the projects whose only
surviving copy of the prose it would destroy:

```
cannot downgrade 050: N focus revision(s) were recorded AFTER the seed and
dropping this table destroys the only copy of the prose they replaced …
Projects: … Export them elsewhere, or rerun with
-x allow_focus_history_downgrade=yes
```

That refusal is the normal case, not an edge case: every write of a focus since
the apply leaves a non-seed row. Export first if the trail matters:

```bash
docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "copy (select * from project_focus_history order by project_key, focus_revision)
     to stdout with csv header;" > /tmp/focus_history_$(date +%F).csv
```

The downgrade also drops the constraint trigger, so arming state does not survive
it. Re-upgrading re-ships the trigger disabled; it inherits nothing.

## Recovering an overwritten focus

Read the trail — newest revision first. An erased focus is stored as NULL and is
the row you are most likely looking for:

```sql
SELECT focus_revision,
       source,
       actor,
       created_at,
       coalesce(focus, '(erased)') AS focus
FROM project_focus_history
WHERE project_key = 'brain-v42'
ORDER BY focus_revision DESC
LIMIT 20;
```

The same thing, from an agent: `brain_focus_history(project_key, limit, offset)`,
which adds the characters added and removed against the revision below.

The prose as it stood just before revision `N` — the recovery query:

```sql
SELECT focus_revision, focus
FROM project_focus_history
WHERE project_key = 'brain-v42'
  AND focus_revision < :n
  AND focus IS NOT NULL
ORDER BY focus_revision DESC
LIMIT 1;
```

`focus IS NOT NULL` matters: without it, an erasure immediately preceding another
erasure hands back a NULL and reads as "there was nothing to recover".

### Putting it back — and why not with an UPDATE

**Restore through `brain_update_project_focus`, not with SQL.** Once the trigger
is armed, a bare `UPDATE project_contexts SET current_focus = …` aborts at COMMIT
with `focus_history_row_missing`: the guard requires an audit row for the
revision the write produces, and a hand-written UPDATE writes none. That is the
guard working, not an obstacle.

If SQL is the only option, both statements must be in ONE transaction, and the
revision must be READ from the write rather than guessed — the 032 trigger
assigns it:

```sql
BEGIN;

WITH written AS (
    UPDATE project_contexts
       SET current_focus = :recovered_prose,
           updated_at = now()
     WHERE project_key = :key
    RETURNING project_key, focus_revision, current_focus
)
INSERT INTO project_focus_history
       (project_key, focus_revision, focus, actor, source)
SELECT project_key, focus_revision, current_focus, :operator, 'maintenance_scrub'
  FROM written
ON CONFLICT (project_key, focus_revision) DO NOTHING;

COMMIT;   -- the deferred trigger checks HERE, not at the UPDATE
```

**`ON CONFLICT DO NOTHING` is not defensive noise, and this runbook learned it the
hard way** — the first version of the statement above did not have it and failed
on the first run. If `:recovered_prose` equals what the row already holds, the
032 trigger does NOT bump the revision (`IS DISTINCT FROM` is false), the write
stays at the revision it was at, and the INSERT collides with the row already
recorded for it:

```
ERROR:  duplicate key value violates unique constraint "project_focus_history_pkey"
DETAIL:  Key (project_key, focus_revision)=(…, 0) already exists.
```

That is not a corner case: re-posting a focus identical to the current one is
exactly what a cautious operator does first. `record_focus_history` carries the
same clause for the same reason.

Measured end to end on `brain_test` with the trigger ARMED — erase, then recover:

```
 focus_revision |        focus        |    actor     |      source
              0 | prose worth keeping | —            | migration_seed
              1 | (erased)            | —            | context_upsert
              2 | prose worth keeping | w44-operator | maintenance_scrub
```

The recovery is itself a revision. It does not rewrite history, it extends it.

`source` must be one of the six the CHECK admits: `session_end`, `focus_tool`,
`context_upsert`, `generic_update`, `maintenance_scrub`, `migration_seed`. A
hand recovery is `maintenance_scrub` — it is not a tool asking, it is an operator
repairing.

The trail itself cannot be edited: `UPDATE` and `DELETE` on
`project_focus_history` are refused by
`project_focus_history_append_only_trigger`. Correcting a bad row means appending
a new revision, never rewriting the old one.

## What this runbook does not cover

The drill has not been run against production — this document was written before
the apply, from measurements taken on `brain_test` (chain 049→050 round-tripped
four times, seed proved on a witness context with a NULL focus, downgrade refused
then accepted under its opt-in). Running it once for real, and dating that run
here, is the step that turns this from a procedure into a rehearsed one.
