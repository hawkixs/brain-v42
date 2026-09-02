# Runbook — PostgreSQL graph ledger and Neo4j projection

**Status: ACTIVE — PRODUCTION CUTOVER VALIDATED ON JULY 22, 2026**

Migrations 033, 034 and 035 carry the canonical ledger, the v2 fencing and the projection
recovery interlock. The production instance cutover was explicitly authorized then
validated with the four gates below. On any other instance, or during a future rebuild,
keep `GRAPH_LEDGER_WRITE_ENABLED=false` outside an explicitly authorized offline window.
During that window, rotation can only arm it after the import and its four preconditions,
with all application writers and normal projectors stopped. Do not reopen any writer before
closing and reviewing the four gates.

PostgreSQL remains the source of truth. Neo4j is a disposable, rebuildable projection.
This runbook applies Brain decision `3d3d72e4-acb7-49fe-aabb-1618e648e627`, option A
"canonical PostgreSQL + rebuild-on-doubt". It covers the upgrade, the legacy import, the
future cutover, observability, restore, rebuild and rollback. It replaces neither a
PostgreSQL restore proof nor an operator authorization.

Two distinct proofs use the word recovery here:

- **PostgreSQL restore tested at the exactly deployed head** proves that the canonical
  ledger, its catalog and its triggers can be recovered into an isolated target. The head 035
  proof remains historical. The DR-v5 run `20260724_150315` renews this gate at head 037 with
  a PostgreSQL 16 restore and 24/24 validated checks.
- **Projection recovery 035** is the crash-safe protocol that interlocks PostgreSQL and
  Neo4j during a rebuild. Its presence in the repository is neither a PostgreSQL restore
  proof, nor a full drill, nor a live deployment.

## Mandatory gates

Stop the procedure as soon as a gate is missing.

| Gate | Production state | Proof to renew |
|---|---|---|
| **PostgreSQL restore at the deployed head** | **Acquired at head 037.** DR-v5 run `20260724_150315` covers eight targets and 47 artifacts; its PostgreSQL 16 restore passes 24/24 checks and matches the independent SQL attestation. | Before each new recovery, revalidate a backup at the effectively deployed head and its invariants. No correlated Neo4j restore is required. |
| **Projection recovery 035** | **Historical proof at head 035.** Recovery `776fd1b9-dbd0-4a1c-b7e3-cd3398ebf93a` returned `recovered`, then the projector armed generation 3. | For any new incident, use a new UUID, unless resuming a still-active `recovery_id`. |
| **Neo4j writer isolation** | **Historical proof from the 035 cutover.** Writers stopped, credential rotated, old credential refused, legacy sessions at zero, and `NEO4J_*` keys removed from the shared runtime. | Reprove quiescence and the refusal of the old credential at every rotation or rebuild. |
| **Full isolated rebuild** | **Historical proof at head 035.** After the smoke test, PostgreSQL and Neo4j agree on 4,678 entities, 11,888 relations and 16,566 cursors; outbox at zero and eleven exact constraints. | Redo the full comparison and an MCP smoke test after every recovery. |

The July 22 graph cutover remains historically validated by its four proofs at head 035.
The DR-v5 run renews the PostgreSQL gate for the then-current production, at head 037. Any
new recovery or new rebuild must revalidate this proof for the targeted instance and head,
then close the three other gates before reopening a writer.
The three historical Neo4j proofs do not close DR-v5's dedicated Neo4j rebuild.

The presence of repository code or tests, unit or integration, closes no gate.
The retained live proof includes the smoke learning
`ca2fac6f-ba19-49e2-a96a-e770e8667c18`, its four relations delivered in both stores, the
DR-v3 backup `20260722_001955`, the DR-v5 run `20260724_150315` and their Brain restore
reports, 24/24. Keep the backup identifiers, deployed versions, bounded outputs and
timestamps of each new proof.

## Active runtime configuration

```dotenv
GRAPH_ENABLED=true
GRAPH_LEDGER_WRITE_ENABLED=true
GRAPH_OUTBOX_INTERVAL_SECONDS=5
GRAPH_OUTBOX_BATCH_SIZE=100
GRAPH_OUTBOX_MAX_ATTEMPTS=10
```

The shared `.env` keeps the flags so that every legacy guard observes the cutover. The keys
`NEO4J_URL`, `NEO4J_USER` and `NEO4J_PASSWORD` must stay absent from the shared `.env` and from
any legacy environment. The versioned pair ties `brain-v42-graph-recon.timer` to
`brain-v42-graph-recon.service`. The service's `ExecStart` only launches the read-only
PostgreSQL inventory of `scripts/rebuild_graph_projection.py`, with no Neo4j credential. The
pair can be scheduled after the verified service is published, but it never replaces an
explicitly attested recovery 035.

The rotated credential belongs solely to the MCP and lives in
`~/.config/brain-v42/graph-projector.env`:

```dotenv
GRAPH_PROJECTOR_ENABLED=true
GRAPH_PROJECTOR_NEO4J_URL=bolt://127.0.0.1:7687
GRAPH_PROJECTOR_NEO4J_USER=neo4j
GRAPH_PROJECTOR_NEO4J_PASSWORD=REPLACE_WITH_ROTATED_PASSWORD
```

Start from `deploy/systemd/graph-projector.env.example`, replace the placeholder without
displaying the secret, then enforce the exact `0600` mode. Reserve the file for the four
`GRAPH_PROJECTOR_*` keys; it must be a regular file, non-symbolic, and owned by the service
user. It must not contain any `NEO4J_*` key. The private URI must use an accepted Bolt/Neo4j
scheme, with no credential, query, fragment or path.

Do not preload the active example file when `GRAPH_LEDGER_WRITE_ENABLED=false`:
`GRAPH_PROJECTOR_ENABLED=true` requires the ledger to be active. The MCP unit loads the private
file last. Its `ExecStartPre` checks any present private file, even with the ledger dormant,
compares the effective ledger flag against the shared file, and refuses a required file that is
absent, too permissive, wrongly owned, symbolic, incomplete, or still a placeholder. The runtime
then starts directly with the attested interpreter, with no login shell able to substitute these
values. This preflight proves neither Neo4j revocation, nor writer quiescence, nor zero
sessions; the credentials gate stays open until operator proofs are supplied.

The Neo4j password must be strong, distinct from `brain_v42_graph` and absent from command
lines, logs and proofs. Never display `POSTGRES_URL`, the Neo4j passwords, or the content of an
`EnvironmentFile`.

### Atomic rotation of the Neo4j credential

The rotation CLI works read-only without `--apply`. From any directory, give it the real,
absolute, non-symbolic path to the canonical repository; it binds every Compose command to that
repository, validates its configuration and verifies that the live container carries that
directory's label. `--shared-env` must designate exactly `/ABSOLUTE/REPO/.env`, the file that
Compose loads automatically. For every Compose command, the CLI forces
`BRAIN_NEO4J_AUTH_FILE=<config-dir>/neo4j-auth` inside a bounded environment with no credential,
then checks the corresponding bind mount after recreation. The preflight creates no file and
modifies neither Neo4j nor the `.env`:

```bash
/ABSOLUTE/REPO/.venv/bin/python \
  /ABSOLUTE/REPO/scripts/rotate_neo4j_credential.py \
  --repo-root /ABSOLUTE/REPO \
  --shared-env /ABSOLUTE/REPO/.env \
  --config-dir /home/SERVICE_USER/.config/brain-v42 \
  --neo4j-uri bolt://127.0.0.1:7687
```

`--config-dir` must be exactly `Path.home()/.config/brain-v42` for the user running the CLI.
The `.config` parent must already exist, be owned by that user, and not be group/other
writable. An existing `brain-v42` directory must already be regular, non-symbolic, owned, and
`0700`: the CLI refuses an incorrect mode instead of fixing it. The shared `.env` must contain a
single effective `GRAPH_ENABLED=true` assignment and no `GRAPH_PROJECTOR_*` key, regardless of
case; those keys belong exclusively to the private file.

If this directory exists with a different mode, prepare it explicitly before the preflight.
Bind every command to this exact path, check type, absence of symlink and current owner, then
revalidate the mode; do not modify or move its content:

```bash
test -d /home/SERVICE_USER/.config/brain-v42
test ! -L /home/SERVICE_USER/.config/brain-v42
test "$(stat -c '%u' /home/SERVICE_USER/.config/brain-v42)" -eq "$(id -u)"
chmod 0700 /home/SERVICE_USER/.config/brain-v42
test "$(stat -c '%a' /home/SERVICE_USER/.config/brain-v42)" = 700
```

The CLI refuses any target other than the local Neo4j on port `7687`, normalizes the
`bolt`/`neo4j` schemes and the `localhost`/`127.0.0.1`/`::1` aliases, then requires it to match
the legacy `NEO4J_URL`. Userinfo, path, query and fragment are forbidden.

Only add `--apply` after validating the four **rotation preconditions**: effective stop of all
writers, proof of zero Neo4j sessions, a dedicated Neo4j target, and a tested PostgreSQL
restore. All four attestations are mandatory and perform no detection of their own:

```bash
/ABSOLUTE/REPO/.venv/bin/python \
  /ABSOLUTE/REPO/scripts/rotate_neo4j_credential.py \
  --repo-root /ABSOLUTE/REPO \
  --shared-env /ABSOLUTE/REPO/.env \
  --config-dir /home/SERVICE_USER/.config/brain-v42 \
  --neo4j-uri bolt://127.0.0.1:7687 \
  --apply \
  --writers-off-confirmed \
  --neo4j-sessions-zero-confirmed \
  --neo4j-dedicated-confirmed \
  --postgres-restore-tested
```

The CLI takes an exclusive lock, creates a resumable journal at `0600`, performs the rotation
with Cypher parameters on the `system` database, then requires that the old credential be
refused with an authentication error and that the new one be accepted. It installs
`neo4j-auth` at `0644` inside the `0700` directory and `graph-projector.env` at `0600`. Only
then does an atomic write remove exactly `NEO4J_URL`, `NEO4J_USER` and `NEO4J_PASSWORD` from
the `.env`, reduce every `GRAPH_LEDGER_WRITE_ENABLED` assignment to the single value `true`,
then recreate **only** the `neo4j` Compose service. The script starts no writer and no systemd
unit.

Every output is a secret-free JSON status object. Never display the `.neo4j-rotation-state`
journal: it contains the material needed for a resume. If the recreation or its validation
fails, the shared `.env` is restored, the journal stays in place, and all writers must remain
stopped. Fix the cause, then resume with the same command plus `--resume`. Never rerun without
`--resume`, delete the journal manually, or reintroduce the old credential.

A `rotated` status does not by itself close the **Projection recovery 035** and **Full isolated
rebuild** gates. Rotation/revocation necessarily precedes them. Then run the recovery and
rebuild described below, keep their proofs, and open no writer before closing the four gates in
the table.

The projector sizes its lease to at least two poll intervals. Lowering
`GRAPH_OUTBOX_MAX_ATTEMPTS` classifies, at the next claim, events already above the new limit
as `max_attempts`; check `exhausted` before and after any change.

With the flag open, startup checks:

- the six tables `projects`, `project_aliases`, `brain_entities`, `entity_relations`,
  `graph_outbox` and `graph_projection_leases`;
- the `neo4j` slot in protocol 2, its armed-generation invariant and its recovery phases;
- the columns `graph_outbox.lease_generation`, `graph_outbox.claim_version`,
  `graph_projection_leases.recovery_id`, `recovery_phase` and
  `last_completed_recovery_id`;
- the validated constraint `graph_projection_leases_recovery_state_valid`.

Startup then creates eleven Neo4j identity constraints, then launches the projector. It does
not check the exact Alembic head, all the 033 triggers, the indexes, or PostgreSQL–Neo4j
consistency. Local validation refuses legacy credentials in the projector role, but does not
prove their revocation on the Neo4j side or their removal from old processes. Operator checks
remain mandatory.

## Initial upgrade to head 035 — historical procedure

This phase documents the initial cutover of July 22, 2026 and installs the graph schema
without changing the runtime owner. Do not replay it on the current production: measure its
head before any action, it already exceeds 035, and never downgrade to reach it. On a new
instance, then finish the Alembic chain up to the current head with the main migration
runbook before activating the current runtime.

1. Keep `GRAPH_LEDGER_WRITE_ENABLED=false`.
2. Explicitly identify the targeted PostgreSQL host and database, then record the current
   head.
3. Take a restorable pre-upgrade PostgreSQL backup for the window's rollback. It does not
   close the restore gate at head 035.
4. Stop MCP, Dream, automation, stdio clients, maintenance scripts and any old projector.
   Beforehand, record the `enabled` state of the timers so it can be restored after the
   window.
5. Plan for the locks: 033 locks the source tables in
   `SHARE ROW EXCLUSIVE`; 034 modifies `graph_outbox` and resets the leases of undelivered
   events; 035 modifies `graph_projection_leases`, adds three columns and a recovery state
   constraint.
6. Apply exactly 035:

   ```bash
   BRAIN_ALEMBIC_ALLOW_PROD=1 alembic upgrade 035
   ```

   `BRAIN_ALEMBIC_ALLOW_PROD=1` deliberately bypasses the production guard. Use it for this
   command only, after verifying the target; never export it in the shell.

7. Check the head and the six tables:

   ```sql
   SELECT version_num FROM alembic_version;

   SELECT to_regclass(names.name) AS relation
   FROM unnest(ARRAY[
       'public.projects',
       'public.project_aliases',
       'public.brain_entities',
       'public.entity_relations',
       'public.graph_outbox',
       'public.graph_projection_leases'
   ]) AS names(name);
   ```

   `version_num` must equal `035` and no relation must be `NULL`.

8. Check PostgreSQL fencing:

   ```sql
   SELECT slot, protocol_version, generation, owner, leased_until,
          neo4j_armed_generation, recovery_id, recovery_phase,
          last_completed_recovery_id
   FROM graph_projection_leases
   WHERE slot = 'neo4j';

   SELECT column_name, is_nullable
   FROM information_schema.columns
   WHERE table_schema = 'public'
     AND table_name = 'graph_outbox'
     AND column_name IN ('lease_generation', 'claim_version')
   ORDER BY column_name;

   SELECT conname, convalidated
   FROM pg_constraint
   WHERE conname = 'graph_projection_leases_recovery_state_valid';
   ```

   Require a `neo4j` row, `protocol_version=2` and
   `neo4j_armed_generation IS NULL OR neo4j_armed_generation=generation`. Outside an active
   recovery, also require `recovery_id IS NULL`, `recovery_phase='idle'`, both outbox columns
   and a `convalidated=true` constraint.

9. After all checks at head 035, take a new post-upgrade PostgreSQL backup. Restore **this
   035 backup**, with no corrective migration in the sandbox, into an isolated target; require
   `alembic_version=035`, then check the full catalog, the 033 triggers, and the 034–035
   invariants. Keep the secret-free proof. As long as this restore has not succeeded, the
   PostgreSQL gate remains open. Do not restore Neo4j to close this gate.

Undelivered outbox events are expected with the flag closed.

## Importing the legacy graph

The import reads a bounded Neo4j snapshot, normalizes identities and allowed properties,
then writes the missing facts into PostgreSQL. It is authorized only before the first
canonical-only write. After that boundary, never reimport Neo4j into PostgreSQL: the
projection can be destroyed and rebuilt, but it no longer decides what is canonical.

1. Preview:

   ```bash
   uv run python scripts/backfill_graph_ledger.py
   ```

2. If `truncated_nodes` or `truncated_relations` is true, explicitly increase
   `--max-nodes` or `--max-relations`, then start over.
3. An unexplained skip blocks activation. Never use `--allow-skips` as cutover proof.
4. For the final import, keep all writers and projectors stopped, then rerun the
   dry-run.
5. Apply only with no skip and no truncation:

   ```bash
   uv run python scripts/backfill_graph_ledger.py \
     --apply \
     --writers-off-confirmed
   ```

`--writers-off-confirmed` is an operator declaration, not a detection. A return of `0`
indicates a complete snapshot, `1` an incomplete report, and `2` a refusal or a failure. Newly
imported facts receive an initial delivered event because they already exist in Neo4j.
Pre-existing PostgreSQL facts stay pending so they converge at cutover.

## Future cutover

Open this window only with operator authorization. The rotation, recovery and rebuild steps
below close the gates in this order; do not start any writer before the four proofs in the
table are valid for the exactly deployed instance and head.

1. Record the deployed revisions, the exactly deployed head — measured before the window,
   never copied from a previous run —, the effective secret-free configuration, the
   PostgreSQL/Neo4j accounts, and the identifier of the PostgreSQL backup whose isolated
   restore was tested at this same head.
   A Neo4j backup is not a gate of option A.
2. Stop Dream, MCP, automation, stdio clients and all direct writers. Also stop
   `brain-v42-graph-recon.timer` and keep it stopped if the effective
   `brain-v42-graph-recon.service` fragment still uses `--fix`.
3. Redo the legacy dry-run, then the final import.
4. Run the preflight then the atomic rotation described above with the four rotation
   preconditions attested.
   Its `rotated` status proves the authenticated refusal of the old credential, the acceptance
   of the new one, the installation of the two files, the removal of the three legacy keys,
   the single `GRAPH_LEDGER_WRITE_ENABLED=true`, the isolated recreation of Neo4j, and its own
   metadata.
5. Verify that no legacy unit or binary still distributes a `NEO4J_*` key, regenerate the
   systemd service from the checkout compatible with the exactly deployed head and the graph
   035 protocol, then run `systemctl --user daemon-reload` and inspect the effective fragment:

   ```bash
   systemctl --user cat brain-v42-graph-recon.service
   ```

   The effective `ExecStart` must call exclusively `<repo>/.venv/bin/python
   <repo>/scripts/rebuild_graph_projection.py`, with no `--fix`, `reconcile_graph_drift`, or
   `recover_graph_projection.py`. With Neo4j Community, writer isolation relies on secret
   distribution and revocation, not on a fine-grained RBAC role.
6. Check in the shared `.env`, without displaying any secret, that `GRAPH_ENABLED=true` is
   kept and that `GRAPH_LEDGER_WRITE_ENABLED=true` appears exactly once.

7. Without reopening a writer, continue with the **Incident and projection recovery 035**
   protocol with a new UUID. For this first cutover, step 4's rotation already satisfies the
   stop, the legacy revocation and the private-secret installation required by steps 1, 2 and
   the beginning of step 7 of the incident protocol: reuse this secret, do not revoke it or
   rerun the rotation. Resume at the step 3 capture, run steps 4 through 6, then at step 7
   launch only the preflight and the recovery. Carry out the full isolated rebuild through to
   convergence and archive the proofs of crash-safe resume, bounded reset, rebuild from
   PostgreSQL and converged counts. These proofs close the two remaining gates; a `rotated`
   status does not replace them.
8. Only after reviewing the four proofs, start a single MCP:

   ```bash
   systemctl --user start brain-mcp-http.service
   ```

9. Verify that the preflight succeeded, then verify startup, the eleven Neo4j constraints,
   the PostgreSQL lease and the Neo4j fence. No `fence_rejected`, `history_conflict`, or
   `batch_failed` log is accepted. A green preflight does not close the credentials gate.
10. On `/metrics`, require `database.graph_outbox.available=true`, `pending=0`, `claimed=0`,
   `exhausted=0`, `oldest_pending_age_seconds=0`, `projector.healthy=true` and
   `projector.recovery_active=false` over a stable window. Then perform a bounded MCP write
   in a test project, and prove the fact in PostgreSQL and its projection in Neo4j.
11. Restart the producers one by one. Check their effective flag before each start and
    watch the outbox. Reactivate Dream last. Never hand the new credential back to an
    old writer.

With the flag open, `scripts/init_graph.py`, `scripts/reconcile_graph.py --fix` and
`scripts.dream.reconcile_graph_drift --fix` must exit with code `2`. This local guard
replaces neither credential rotation nor quiescence.

## Observability

The CLI provides a read-only inventory:

```bash
uv run python scripts/rebuild_graph_projection.py
```

This command requires schema 035. Its old `--apply` mode has been removed and refuses any
mutation; only `scripts/recover_graph_projection.py` carries the recovery protocol.

### PostgreSQL

```sql
SELECT
    count(*) FILTER (
        WHERE delivered_at IS NULL
          AND last_error_code IS DISTINCT FROM 'max_attempts'
    ) AS pending,
    count(*) FILTER (
        WHERE delivered_at IS NULL
          AND last_error_code = 'max_attempts'
    ) AS exhausted,
    min(created_at) FILTER (
        WHERE delivered_at IS NULL
          AND last_error_code IS DISTINCT FROM 'max_attempts'
    ) AS oldest_pending
FROM graph_outbox;

SELECT operation, attempt_count, COALESCE(last_error_code, 'none') AS error_code,
       lease_generation, claim_version, lease_owner, leased_until, count(*)
FROM graph_outbox
WHERE delivered_at IS NULL
GROUP BY operation, attempt_count, COALESCE(last_error_code, 'none'),
         lease_generation, claim_version, lease_owner, leased_until
ORDER BY operation, attempt_count, error_code, leased_until;

SELECT clock_timestamp() AS db_now,
       slot, protocol_version, generation, neo4j_armed_generation,
       owner, leased_until, recovery_id, recovery_phase,
       last_completed_recovery_id,
       CASE
           WHEN recovery_id IS NOT NULL THEN 'recovery_' || recovery_phase
           WHEN neo4j_armed_generation = generation THEN 'armed'
           WHEN leased_until > clock_timestamp() THEN 'activating'
           ELSE 'unarmed_requires_recovery_check'
       END AS state
FROM graph_projection_leases
WHERE slot = 'neo4j';
```

An unarmed PostgreSQL generation always requires a comparison against Neo4j. Do not infer a
repair from the PostgreSQL row alone.

### Neo4j

```cypher
MATCH (fence:BrainProjectionFence {name: 'canonical'})
RETURN fence.protocol_version AS protocol_version,
       fence.generation AS generation,
       fence.owner_id AS owner_id,
       fence.recovery_id AS recovery_id;

MATCH (cursor:BrainProjectionCursor)
RETURN count(cursor) AS cursor_count,
       max(cursor.updated_at) AS latest_cursor_update;
```

The absence of the fence blocks normal runtime: starting MCP no longer creates it. It is
accepted only on an empty, dedicated Neo4j target during an authorized recovery 035. A normal
unarmed PostgreSQL leader can only advance from the generation immediately preceding the Neo4j
one; an already-armed leader requires its exact generation. A persistent divergence, an owner
conflict, an unexpected recovery marker, or an incompatible cursor history blocks the
procedure. This check does not detect a Neo4j PITR within the same generation; any known
restore therefore requires a full recovery and requeue.

### Logs

```bash
journalctl --user -u brain-mcp-http.service --since '-30 min' --no-pager \
  | rg 'graph_outbox_projector|fence_rejected|history_conflict|batch_failed'
```

Handle immediately:

- `fence_rejected`: inconsistent generation or incomplete activation;
- `history_conflict`: same revision, different history;
- repeated `batch_failed`: projection unavailable;
- `exhausted>0`, growth in `pending`, or aging of `oldest_pending`.

The JSON `/metrics` endpoint also exposes a fixed-cardinality block:

```json
{
  "database": {
    "graph_outbox": {
      "available": true,
      "pending": 0,
      "ready": 0,
      "claimed": 0,
      "exhausted": 0,
      "oldest_pending_age_seconds": 0.0,
      "projector": {
        "generation": 12,
        "armed": true,
        "lease_active": true,
        "recovery_active": false,
        "healthy": true
      }
    }
  }
}
```

`pending` includes deferred retries but excludes `exhausted`; `ready` counts events that are
temporally available and have no live lease, even though aggregate ordering can still block
them, and `claimed` counts those with a live lease. The difference
`pending - ready - claimed` therefore groups scheduled backoff and revisions blocked by
ordering. `projector.healthy` means
"generation armed, lease alive, no active recovery"; this signal alone does not prove the
Neo4j content. `available=false` is a hard no-go: the associated default zeros constitute no
proof. The repository keeps this existing JSON contract and adds no `prometheus_client`
dependency.

## Incident and projection recovery 035

An unarmed PostgreSQL generation, a `history_conflict`, a lost or doubtful projection, or an
interrupted recovery marker require the 035 protocol. Never force arming, modify a generation,
delete the fence, empty Neo4j, or manually reset the cursors.
The only published mutating path for a recovery/rebuild is
`scripts/recover_graph_projection.py`.

This procedure first requires a PostgreSQL restore at the exactly deployed head — measured
before the procedure, never copied from a previous run —, writer isolation, zero sessions, and
a dedicated Neo4j target. Its crash-safe completion and the rebuild's convergence then close
the two corresponding gates. The `--postgres-restore-tested` flag is an operator declaration;
migration 035 or a green test does not satisfy it.

1. Stop MCP, Dream, automation, stdio clients, maintenance, and all writers/projectors.
2. Revoke the current Neo4j credential, remove its legacy distribution, and attest zero
   active sessions on the targeted Neo4j database, across all credentials.
3. Capture without mutation the PostgreSQL lease/outbox, the Neo4j fence/cursors, the logs,
   the versions, and the PostgreSQL backup identifier.
4. Check the proof of an isolated PostgreSQL restore at the exactly deployed head, with the
   033–035 graph invariants and all objects from the following revisions. If this proof is
   missing, targets an earlier head, or the restore failed, stop; never downgrade to close
   this gate.
5. Prove that the targeted Neo4j database is dedicated to Brain. Option A treats this database
   as a disposable projection: start from an empty target or accept that the recovery erases
   only the allowlisted Brain labels and the cursors. A Neo4j backup is not required and must
   never be used to restore the canonical state.
6. Generate and archive a unique UUID for this incident. If PostgreSQL already contains a
   `recovery_id`, resume exactly that one, even after its lease has expired; another UUID is
   refused. Do not reuse the UUID of an old recovery after a more recent recovery: only the
   last completion is remembered.
7. Install a new secret only in the private MCP file. In the shared `.env`, configure
   `GRAPH_ENABLED=true` and `GRAPH_LEDGER_WRITE_ENABLED=true`, without starting any service.
   Replace the absolute paths and the UUID below, run the preflight, then launch a transient
   user unit that loads both `EnvironmentFile` entries without interpreting them as shell
   code:

   ```bash
   /ABSOLUTE/REPO/.venv/bin/python \
     /ABSOLUTE/REPO/scripts/check_graph_projector_env.py \
     --shared /ABSOLUTE/REPO/.env \
     --private /home/SERVICE_USER/.config/brain-v42/graph-projector.env

   systemd-run --user --wait --pipe --collect --service-type=exec \
     --unit=brain-v42-graph-recovery \
     --working-directory=/ABSOLUTE/REPO \
     --property=EnvironmentFile=/ABSOLUTE/REPO/.env \
     --property=EnvironmentFile=/home/SERVICE_USER/.config/brain-v42/graph-projector.env \
     /ABSOLUTE/REPO/.venv/bin/python \
     /ABSOLUTE/REPO/scripts/recover_graph_projection.py \
     --apply \
     --recovery-id UUID_GENERATED_FOR_THIS_INCIDENT \
     --writers-off-confirmed \
     --legacy-credential-revoked-confirmed \
     --neo4j-sessions-zero-confirmed \
     --neo4j-dedicated-confirmed \
     --postgres-restore-tested
   ```

   The five confirmations are operator assertions, not automatic checks. Before creating the
   PostgreSQL interlock, the CLI checks the private connectivity to Neo4j, then requests the
   creation of the projection constraints. A failure at this stage refuses the recovery
   without a PostgreSQL interlock. Audit `SHOW CONSTRAINTS` separately: the CLI does not
   reread their definition when a name already exists.

   The lease defaults to 3600 seconds and accepts an explicit value from 60 to 86400 via
   `--lease-seconds`. **DANGER:** the reset deletes the nodes carrying the Brain labels
   `Project`, `Domain`, `Decision`, `Learning`, `Snippet`, `Runbook`, `ADR`, `Feature` or
   `Plan`, as well as the `BrainProjectionCursor` nodes, before rebuilding them from
   PostgreSQL. It does not delete the other labels, but the dedicated database remains
   mandatory.
8. If the process is interrupted, rerun the full command with the same UUID. The protocol
   resumes at `prepared` or `neo_ready` with no new bump and no new requeue. In `neo_ready`,
   it **always** replays the bounded reset before finalizing: a surviving fence or cursors do
   not prove content integrity. An empty target, an exact marker, a compatible older
   generation, or the exact fence already finalized by the same owner are accepted; a future
   fence, a wrong protocol, or a foreign marker remains refused. A resume after completion
   returns `already_completed` without touching Neo4j. If PostgreSQL has returned to
   `idle`/`completed` and Neo4j was subsequently lost, open a new incident with a new UUID:
   never replay the old completed UUID.
9. Archive the secret-free JSON. The `recovered` status means "reset and requeue finalized",
   not "projection already converged". Preparation increments the generation and requeues the
   revisions in a PostgreSQL transaction; the reset erases the bounded Brain labels and the
   cursors, then installs the recovery marker in a Neo4j transaction; finalization removes
   both interlocks. A more recent fence, a wrong protocol, or another UUID must remain a
   fail-closed refusal.
10. Start a single MCP with the ledger and the private credential active. Check fence, lease,
    logs, `pending=0`, `exhausted=0`, then compare the canonical counts against the Neo4j
    business nodes:

    ```sql
    SELECT count(*) AS entity_count
    FROM brain_entities
    WHERE lifecycle <> 'deleted';

    SELECT count(*) AS relation_count
    FROM entity_relations
    WHERE lifecycle = 'active';
    ```

    ```cypher
    MATCH (node)
    WHERE NOT node:BrainProjectionFence
      AND NOT node:BrainProjectionCursor
    RETURN count(node) AS entity_count;

    MATCH ()-[relation]->()
    RETURN count(relation) AS relation_count;
    ```

    The `BrainProjectionFence` and `BrainProjectionCursor` nodes are internal controls;
    counting them as entities would make the comparison wrong.
11. Sample each relation type and perform a bounded MCP smoke test before reopening the
    producers one by one.

## Restore

PostgreSQL is the sole restore authority. Neo4j is rebuilt from that state and is never
restored as a correlated participant. The DR-v5 run `20260724_150315` satisfies this
prerequisite for the then-current production, at head 037. Any new backup, head, or
environment must obtain its own isolated validation before applying this procedure.

1. Stop all owners and revoke their Neo4j credential.
2. Restore PostgreSQL into an isolated target.
3. Require the exactly deployed head — measured before the procedure, never copied from a
   previous run — and check the catalog, the 033 triggers, the 034–035 constraints, the
   036–037 objects, and the application invariants.
   Keep the proof; if the isolated restore fails or announces an earlier head, stop.
4. Explicitly designate the restored target as the new canonical authority before any
   mutation. Create for this window a dedicated shared environment file whose `POSTGRES_URL`
   targets this instance; do not implicitly reuse the live `.env`. From this same
   environment, record without secret `current_database()`, `inet_server_addr()`,
   `inet_server_port()`, and the exact expected value of `alembic_version.version_num`. In the
   recovery command from the previous section, replace every `/ABSOLUTE/REPO/.env` with this
   dedicated file. Any ambiguity about the endpoint or the database is a hard no-go.
5. Do not start the projector and do not reuse a potentially more recent Neo4j. Prepare a
   dedicated, empty Neo4j database with a new credential.
6. Examine the restored PostgreSQL singleton. Wait for a runtime lease to expire without
   rewriting it. If a `recovery_id` is already present, resume that exact UUID instead of
   creating another one.
7. Choose the UUID from PostgreSQL. If `recovery_id` is active at `prepared` or `neo_ready`,
   resume exactly that UUID. A `neo_ready` resume systematically replays the bounded reset on
   the empty, older, or exactly compatible target; it refuses a more recent fence, a wrong
   protocol, or a foreign marker. If PostgreSQL is `idle` and the last recovery is only
   remembered in `last_completed_recovery_id`, generate a new UUID: the old one would return
   `already_completed` without rebuilding Neo4j.
8. Issue a new credential, prove zero active sessions on the targeted database, install the
   private file, then run the 035 recovery from the previous section. It requeues all
   revisions and rebuilds Neo4j from certified PostgreSQL.
9. Start a single MCP. Require finalized fence and lease, convergence, corrected counts, and
   a smoke test before reopening the writers.

Never restore Neo4j or reimport its content into PostgreSQL. Any loss, PITR, or doubt about
the projection requires a full recovery 035 from the current PostgreSQL. Same generation does
not mean same content: the runtime does not detect an intra-generation Neo4j PITR.

## Rollback

### Runtime rollback

Keep the schema at the exactly deployed head — measured before the rollback, never copied
from a previous run. A graph runtime rollback authorizes no Alembic downgrade.

Production crossed the first canonical-only write on July 22, 2026 with the smoke
`ca2fac6f-ba19-49e2-a96a-e770e8667c18`. The conditional legacy branch of step 2 and step 3
describe only a pre-canonical window and no longer apply to this instance. Revoking the
projector credential remains mandatory; PostgreSQL remains the authority and any rebuild goes
through a recovery 035.

1. Stop Dream, MCP, automation, and all writers.
2. Revoke the projector's credential. If the legacy path must resume, issue a new rollback
   credential; never reactivate the old secret.
3. Before the first canonical-only write, the legacy path can resume with a new secret after
   removing the private projector file, setting
   `GRAPH_LEDGER_WRITE_ENABLED=false`, and checking consistency.
4. As soon as a canonical-only write has been accepted, PostgreSQL remains the authority.
   **Do not** restore Neo4j, reimport its projection, or simply set
   `GRAPH_LEDGER_WRITE_ENABLED=false` back. Fix the ledger then run a recovery 035 toward a
   dedicated, empty Neo4j database.
5. Then restart a single owner compatible with the chosen branch, and reopen the producers
   one by one. A private projector file still present with
   `GRAPH_PROJECTOR_ENABLED=false` is refused by the preflight; leaving it at `true` with the
   ledger closed is also invalid.
6. Only reactivate `brain-v42-graph-recon.timer` after inspecting the effective
   `brain-v42-graph-recon.service` fragment after `daemon-reload` and validating the exact
   read-only `ExecStart` above. Never redirect this service toward recovery 035.

The 033–035 tables, triggers and constraints stay in place. With the flag closed, the outbox
can keep receiving events from the business tables with no projector. Relations written
directly into Neo4j can stay absent from the ledger; any new cutover requires the four gates
of option A and a new legacy reconciliation.

### Schema downgrade

A downgrade is never a runtime rollback:

- a downgrade to 034 removes the recovery 035 interlock; the current active ledger runtime
  refuses this schema;
- a downgrade to 033 removes the v2 lease and the claims columns; the current ledger runtime
  can no longer start;
- a downgrade to 032 then removes the five 033 tables, i.e. six graph/fencing tables in
  total, their facts, and the outbox history;
- the alias normalization already applied to project keys is not restored.

Only run a downgrade after a backup, a ledger export, a full stop, credential revocation, and
explicit authorization of the loss.

After any rollback, keep: secret-free configuration, service and timer state, Alembic head,
PostgreSQL/Neo4j generations, counters, outbox inventory, revoked credentials, backup
identifiers, and the smoke test result.
