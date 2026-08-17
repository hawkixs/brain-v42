# Canonical Multi-Project Plan Index Repair

**Date:** 2026-07-28

**Status:** Approved for implementation

**Ticket:** `44ee7643-fb06-4186-a364-cb175610b973`

**Roadmap:** `Plans Chunking & Search Integration — Design`

## Problem

Seven project contexts configure relative `plan_scan_paths`. The systemd service runs from the
Brain repository, so one project's scan can read another project's files. The indexer also checks
an exact `file_path` without filtering by `project_key`. A relative path can therefore make the
first indexed owner suppress every later owner.

A read-only production inventory on 2026-07-28 found only relative rows for the affected projects:

| Project | Indexed rows | Canonical local rows |
| --- | ---: | ---: |
| `red-games` | 23 | 0 |
| `red-phone` | 14 | 0 |
| `red-quant` | 42 | 0 |
| `red-writer` | 37 | 0 |
| `red-viewer` | 0 | 0 |
| `red-shrik` | 0 | 0 |
| `red-gift` | 0 | 0 |

Historical ticket counts are evidence, not permanent constants. Repositories have changed since
the ticket opened. The repair must recompute each current corpus and report any difference before
it can mutate data.

## Goals

1. Stop future indexing through relative, missing, unreadable, or non-directory scan paths.
2. Scope unchanged-file detection to the requested project.
3. Inventory the seven contexts, their canonical files, their indexed rows, and every collision
   without changing PostgreSQL.
4. Replace only `plan_scan_paths`; preserve every other project-context field.
5. Index and verify canonical rows before removing polluted rows.
6. Require a complete control snapshot, compare-and-swap checks, a verified backup receipt, and a
   tested restore attestation before any mutating phase.
7. Keep every mutation bounded, transactional, resumable, and content-safe in logs.

## Non-goals

- Run the production repair, restart services, deploy units, or change ticket status in this code
  delivery.
- Infer repository roots from the process working directory.
- Treat `brain_set_project_context` as a partial update.
- Change the global `indexed_plans.file_path` uniqueness contract.
- Modify Dream, embedding deployment, migrations 036/037, or unrelated project contexts.

## Selected design

### 1. Canonical-path gate in `PlanIndexer`

`index_path()` will canonicalize each input with `Path.resolve(strict=True)` before reading files.
It will accept only absolute, readable, searchable directories. A relative or invalid path raises a
typed path error before globbing.

`index_project()` will catch that typed error per configured path, increment `errors`, and continue
with other valid paths. It will log `project_key`, `file_path`, and a closed `reason_code`; it will
not log file contents or exception text. Every persisted `file_path` and `metadata.source_file`
will use the canonical absolute path.

`_is_unchanged()` will add `project_key` to its exact-path query. Its duplicate-by-hash shortcut
will suppress a candidate only when the existing row belongs to the same project and names a live,
absolute file. Relative legacy rows will never suppress canonical indexing.

### 2. Explicit repair manifest

The maintenance command will consume a versioned JSON manifest. The manifest maps each allowed
project key to explicit absolute scan directories. It contains no database credentials and no
implicit working-directory rule.

The command accepts exactly the seven ticket projects. It rejects duplicate projects, duplicate
paths, symlinks that resolve outside their declared project root, missing directories, unreadable
directories, and paths that remain relative after parsing. The default command mode is read-only
inventory.

### 3. Complete control snapshot

Inventory writes an operator-owned snapshot with mode `0600`. The snapshot contains:

- every column of each targeted `project_contexts` row;
- its canonical serialization and SHA-256 CAS fingerprint;
- the proposed canonical scan paths;
- every matching local plan path, content hash, size, and project owner;
- every targeted `indexed_plans` identity, owner, path, content hash, and lifecycle status;
- chunk counts and matching `feature_artifacts` link identities;
- the exact polluted-row set, missing canonical set, and cross-project collision set;
- the Alembic revision and a fingerprint of the database identity without credentials.

The snapshot excludes plan contents, embeddings, environment values, credentials, and connection
strings. It supports concurrency checks and bounded deletion; it is not the data backup.

### 4. Verified backup gate

Every mutating phase requires all of the following:

- the snapshot file and its expected SHA-256;
- a backup receipt file and its expected SHA-256;
- an explicit `--postgres-restore-tested` attestation;
- an explicit `--writers-off-confirmed` attestation;
- production Alembic head 037, with migrations 036 and 037 present.

The command records only receipt identifiers and digests in its secret-safe result. A missing,
changed, unreadable, or overly permissive proof file blocks mutation.

### 5. Copy, verify, then prune

The repair has separate, replay-safe phases:

1. **Inventory:** read PostgreSQL and the filesystem, write the control snapshot, and report exact
   counts. This is the default.
2. **Apply paths:** start a serializable transaction, lock the seven context rows, recompute their
   fingerprints, and compare them with the snapshot. Update only `plan_scan_paths` when every CAS
   check passes. Roll back the whole transaction on any mismatch.
3. **Reindex:** run the existing indexer project by project against canonical paths. Any non-zero
   error count blocks finalization.
4. **Finalize:** start a serializable transaction and repeat the CAS checks. Require one correctly
   owned canonical row with the expected content hash for every local file. Reject missing files,
   extra canonical rows, hash drift, ownership collisions, or changed polluted rows. Delete
   matching `feature_artifacts` links first, then delete only the exact polluted plan IDs captured
   by the snapshot. Plan chunks cascade from `indexed_plans`. Commit only when post-delete counts
   match the computed inventory.

Finalization never trusts a filename alone. Project ownership, canonical path, content hash, row
identity, and the snapshot digest must all match.

### 6. Rollback and recovery

Before finalization, rollback restores the complete project-context state from the snapshot with a
serializable CAS transaction. It verifies every column, then rewrites only `plan_scan_paths` and
`updated_at`, the two fields changed by apply. It removes only canonical rows created after the
snapshot and proven to belong to this repair.

After finalization, the verified PostgreSQL backup is the authoritative rollback because the
control snapshot deliberately omits contents and embeddings. The command prints the exact backup
receipt required by the runbook and refuses to claim an in-place full restore.

Every failure leaves the snapshot and backup proof untouched. Re-running a phase with the same
snapshot is idempotent or fails closed on drift.

## Interfaces

Implementation will add a maintenance service under
`src/brain_v42/maintenance/plan_index_repair.py` and a thin CLI under
`scripts/repair_plan_index.py`. The service owns manifest validation, inventory, fingerprints,
transaction boundaries, verification, and rollback. The CLI owns argument validation and
secret-safe JSON output.

The command will expose `inventory`, `apply-paths`, `verify`, `finalize`, and
`rollback-before-finalize`. It will not offer a one-step destructive shortcut.

## Error handling and observability

The CLI returns `0` only when the requested phase completes and all gates pass. Invalid input or a
safety-gate failure returns `2`; an operational failure returns `1`. Unexpected exceptions cross a
single masking boundary that emits only the exception type and a repair run identifier.

Reports include project keys, canonical paths, reason codes, counts, row IDs, and content hashes.
They exclude plan contents, embeddings, environment values, connection strings, and raw exception
messages.

## Tests

TDD will cover:

- rejection of relative, missing, unreadable, file, and escaping-symlink paths;
- canonical paths persisted in plan rows and metadata;
- project-scoped exact-path and duplicate-by-hash checks;
- deterministic, read-only inventory and private snapshot permissions;
- complete project-context fingerprints and preservation of unrelated fields;
- backup, restore-attestation, migration-head, and writers-off gates;
- CAS conflicts before each mutation;
- transaction rollback on partial failure;
- canonical completeness, hash, owner, extra-row, and error-count failures;
- bounded feature-link and plan deletion;
- pre-finalize rollback and idempotent replay;
- content-safe logs and CLI failures.

Database-backed tests will use only `BRAIN_V42_TEST_DB_URL` and will fail closed when it is absent
or points at the production database. Unit tests will use temporary repositories and mocked
sessions. No test may mutate production.

## Acceptance criteria

The code delivery succeeds when:

- the indexer cannot scan a relative path and reports the error without leaking content;
- unchanged detection cannot cross project ownership;
- the default repair command produces the exact dry-run inventory and changes no external state;
- mutating phases require the unchanged snapshot, verified backup proof, restore attestation,
  writers-off attestation, and schema gate;
- the final delete set is exact and can run only after canonical corpus verification;
- tests prove field preservation, CAS, transactions, rollback, isolation, counts, and safe logs;
- the operator runbook states that production apply, reindex, finalization, restart, and ticket
  transition remain separate authorized actions.
