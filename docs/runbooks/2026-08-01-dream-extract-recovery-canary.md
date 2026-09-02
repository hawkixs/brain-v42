# Controlled EXTRACT Dream recovery

## Scope

This runbook prepares the recovery of missing embeddings and the Dream canary. It must not be run within this task.

## Owner and cadence

Brain operations owns `brain-v42-embedding-backfill.service`. The planned timer runs every day at 04:30 with a maximum batch of 100 entities, in groups of 20. The service is neither installed nor enabled by this change.

## Cause and recovery strategy

The EXTRACT deduplication barrier refuses any active learning or decision whose
embedding is missing or has a norm less than or equal to `1e-6`: such a vector
is not comparable by cosine. Historically, the backfill worker only
selected and replaced `NULL` values. A zero-norm value therefore
stayed permanently in the active corpus and made every EXTRACT fail.

The backfill and its metrics now select the same definition of
non-comparability as the Dream barrier, and its compare-and-set also replaces
a zero-norm vector only if `updated_at` is unchanged. The barrier
stays fail-closed: if the repair does not produce a comparable corpus,
EXTRACT persists a `failed` attempt with a redacted cause and neither
creates nor applies a proposal.

Within an operator window, check the backlog on an isolated database then work it
down in bounded batches; the second pass must store zero embeddings. The
CLI reads `POSTGRES_URL` (not `BRAIN_V42_TEST_DB_URL`). The sequence kept
below is the only path that authorizes `--execute`: it binds the snapshot,
validates an isolated restore, then launches the worker.

## Snapshot and rollback of backfill writes

The backfill replaces `embedding` and `updated_at` in place. It has neither a
log of old values nor a persistent batch identifier; the
`embedding_backfill.*` metrics are aggregated. Without a prior snapshot, a
granular rollback per learning or decision is therefore impossible. Do not claim
to reconstruct an old vector from the worker's report.

Before any `--execute` run, explicitly identify the scope
(`project_key`, types, `limit`, date/time) and reserve an empty restore
database. URIs must never be displayed. The guard canonicalizes the
identities with `sqlalchemy.engine.make_url`, like the Alembic resolver:
it rejects all URI parameters, which could redefine the host, the
port or the effectively opened database. It then compares exactly
the `host/port/database name` of `POSTGRES_URL` and `BACKFILL_PGURL`.

`BACKFILL_RESTORE_PGURL` remains explicit, but the guard checks that it targets
exactly `BACKFILL_RESTORE_DB`, that it is distinct from the operated database, and that
its host/port is the same as the admin URI passed to `createdb`. The
validated restore is therefore a blocking prerequisite of `--execute`, not an
optional step.

<!-- backfill-recovery-guard:start -->
```bash
set -euo pipefail
: "${POSTGRES_URL:?URI asyncpg de la base opérée requise}"
: "${BACKFILL_PGURL:?URI libpq de la base à snapshotter requise}"
: "${BACKFILL_PROJECT:?project_key explicite requis}"
: "${BACKFILL_SNAPSHOT_DIR:?répertoire de snapshots explicite requis}"
: "${BACKFILL_RESTORE_DB:?nom de base isolée vide requis}"
: "${BACKFILL_RESTORE_ADMIN_PGURL:?URI libpq d’administration requise}"
: "${BACKFILL_RESTORE_PGURL:?URI libpq de restauration requise}"

# Logs neither URI nor secret. Any mismatch exits before pg_dump, pg_restore
# and the worker. make_url also rejects parameters that could change the target.
"${BACKFILL_PYTHON:-python}" - \
  "$POSTGRES_URL" "$BACKFILL_PGURL" "$BACKFILL_RESTORE_ADMIN_PGURL" \
  "$BACKFILL_RESTORE_PGURL" "$BACKFILL_RESTORE_DB" <<'PY'
from sqlalchemy.engine import make_url
import sys


def identity(raw: str, label: str, *, asyncpg: bool = False) -> tuple[str, int, str]:
    try:
        url = make_url(raw)
    except Exception:
        raise SystemExit(f"{label} database identity is invalid") from None
    if url.query:
        raise SystemExit(f"{label} database identity must not include query parameters")
    expected_driver = "postgresql+asyncpg" if asyncpg else "postgresql"
    if url.drivername != expected_driver:
        raise SystemExit(f"{label} database identity has an invalid driver")
    if not url.host or url.port is None or not url.database:
        raise SystemExit(f"{label} database identity must include host, port, and database")
    return (url.host.casefold(), url.port, url.database)


target = identity(sys.argv[1], "backfill target", asyncpg=True)
snapshot = identity(sys.argv[2], "snapshot target")
restore_admin = identity(sys.argv[3], "restore administration")
restore = identity(sys.argv[4], "restore target")
restore_db = sys.argv[5]

if snapshot != target:
    raise SystemExit("snapshot target identity mismatch")
if restore_admin[:2] != restore[:2]:
    raise SystemExit("restore administration identity mismatch")
if restore[2] != restore_db:
    raise SystemExit("restore database name mismatch")
if restore == target:
    raise SystemExit("restore target must differ from operated database")
PY

mkdir -p "$BACKFILL_SNAPSHOT_DIR"
pg_dump --format=custom --no-owner --file \
  "$BACKFILL_SNAPSHOT_DIR/brain-pre-backfill.dump" "$BACKFILL_PGURL"
pg_restore --list "$BACKFILL_SNAPSHOT_DIR/brain-pre-backfill.dump" >/dev/null
sha256sum "$BACKFILL_SNAPSHOT_DIR/brain-pre-backfill.dump"

# createdb fails if the name already exists: it therefore cannot mistakenly restore
# into a pre-existing database. The SQL check confirms the isolated database before the worker.
createdb --maintenance-db="$BACKFILL_RESTORE_ADMIN_PGURL" -- "$BACKFILL_RESTORE_DB"
pg_restore --no-owner --dbname="$BACKFILL_RESTORE_PGURL" \
  "$BACKFILL_SNAPSHOT_DIR/brain-pre-backfill.dump"
psql "$BACKFILL_RESTORE_PGURL" -v ON_ERROR_STOP=1 -v restore_db="$BACKFILL_RESTORE_DB" -Atqc \
  "SELECT current_database() = :'restore_db';" | grep -qx t

"${BACKFILL_PYTHON:-python}" -m brain_v42.maintenance.embedding_backfill \
  --execute --project-key "$BACKFILL_PROJECT" --entity-type learning --entity-type decision \
  --batch-size 20 --limit 100
```
<!-- backfill-recovery-guard:end -->

The hash receipt, the scope, the identity equality and the isolated restore are the
prerequisites for execution. On the slightest failure of the worker, of validation
or of ambiguity about the target, stop the canary and do not rerun `--execute`: the
Dream barrier stays fail-closed.

Restoring the operated database requires an approved full-database recovery
procedure, writers stopped and the snapshot validated above. Use the
restored isolated database as proof before preparing a replacement database
per the approved DR procedure; never run `pg_restore` in place on the
operated database. There is no granular rollback per learning/decision, no
safe native selective restore command for this worker, and no way to
recover old vectors without the snapshot. Without these prerequisites, stop
rather than overwrite concurrent writes.

## Dream canary

After applying migration 038, run EXTRACT twice in DRY with
a single window:

```bash
POSTGRES_URL=<url-asyncpg-base-isolee> \
  BRAIN_NVIDIA_API_KEY=<cle-canary> \
  python -m scripts.ticket_extract --limit 1
```

Verify, for each of the two passes, a terminal `dream_runs` line with no
error, one attempt per ticket, no applied proposal (`--wet` absent)
and a comparable learning/decision backlog of zero. Only increase the number
of tickets after two clean canaries.

## Stop

Stop the canary if the backfill returns an error, if a terminal `dream_run` is missing, or if an attempt contains an unredacted cause. Do not enable the timer without operator validation.
