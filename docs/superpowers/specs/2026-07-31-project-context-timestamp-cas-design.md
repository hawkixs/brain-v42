# Timestamp CAS for `project_contexts`

**Date:** 2026-07-31

**Status:** decision approved

**Ticket:** `44ee7643-fb06-4186-a364-cb175610b973`

## Decision and root proof

The repair must sign then replay `updated_at` exactly, along with `plan_scan_paths`. Real
PostgreSQL produced three compliant proofs and two failures: the shared `BEFORE UPDATE` trigger overwrote
the signed timestamp by `+23,727 µs`, then `finalize` returned `context_cas_conflict`.

The cause is `trg_project_contexts_updated`, attached to the generic function
`update_updated_at`, shared by eight tables. After Dream migration 038, migration 039
gives `project_contexts` a dedicated function without modifying the historical function or its seven
other bindings: `decisions`,
`learnings`, `snippets`, `runbooks`, `adrs`, `features` and `indexed_plans`.

## Goals and non-goals

The solution keeps an exact CAS, restores exactly the paths and the non-null timestamp of the
snapshot, preserves the ordinary writers' auto-timestamp, and isolates the change to
`project_contexts`.

`updated_at` is `NOT NULL`. The contract promises neither preservation nor rollback of `NULL`: under the
GUC set exactly to `on`, an explicit `NULL` value fails atomically; without the GUC or with an
invalid value, it is replaced by `CURRENT_TIMESTAMP`. The change does not modify the plan corpus,
the operator phases, the seven other tables, or the backup receipt.

## Rejected alternatives

| Alternative | Rejection |
| --- | --- |
| Backup receipt with `RETURNING` | Too complex and incomplete to prove the exact rollback of both fields. |
| CAS that ignores `updated_at` | Weakens the concurrency contract. |
| Modifying `update_updated_at` | Forbidden: eight tables share this function. |

## Chosen architecture

Migration `039`, with `down_revision = "038"`, creates the function
`public.set_project_context_updated_at()` without `OR REPLACE`, as `SECURITY INVOKER`. It remaps the existing trigger
`trg_project_contexts_updated` to this function.

The dedicated function is exactly `public.set_project_context_updated_at`, with `pronargs = 0`,
return `trigger`, `prosecdef = false`, volatility `VOLATILE`, and parallel safety `PARALLEL UNSAFE`.
The following DDL is its canonical source:

```sql
CREATE FUNCTION public.set_project_context_updated_at()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY INVOKER
AS $function$
BEGIN
    IF current_setting('brain_v42.allow_explicit_project_context_updated_at', true) = 'on' THEN
        IF NEW.updated_at IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23502',
                MESSAGE = 'explicit_project_context_updated_at_null';
        END IF;
        RETURN NEW;
    END IF;
    NEW.updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$function$;
```

The canonical representation is the raw UTF-8 bytes of `pg_proc.prosrc`, without normalization. The
comparison query is:

```sql
SELECT
    encode(pg_catalog.sha256(pg_catalog.convert_to(prosrc, 'UTF8')), 'hex') AS prosrc_sha256,
    octet_length(pg_catalog.convert_to(prosrc, 'UTF8')) AS prosrc_octets
FROM pg_catalog.pg_proc
WHERE oid = 'public.update_updated_at()'::regprocedure;
```

The same predicate with `public.set_project_context_updated_at()` compares the dedicated function. The
literal PostgreSQL 16.14 hashes observed are:

| Function | SHA-256 `prosrc` | Bytes |
| --- | --- | ---: |
| `public.update_updated_at()` | `83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59` | 96 |
| `public.set_project_context_updated_at()` | `60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419` | 391 |

The dedicated `prosrc` begins and ends with LF. Its observed attributes are schema `public`, language
`plpgsql`, `prokind = 'f'`, `provolatile = 'v'`, `proparallel = 'u'`, `prosecdef = false`,
`proleakproof = false`, `proisstrict = false`, `proretset = false`, `pronargs = 0`,
`pronargdefaults = 0`, empty `proargtypes`, `prorettype = trigger`, and `proconfig IS NULL`. The
historical function has the same relevant attributes already captured; owner and ACL remain
separate contracts. `pg_get_functiondef` is kept for diagnostics, never for the hash.

The captures come from disposable PostgreSQL 16.14 containers, with no production access. The
resources were deleted and their absence verified. This function proof does not constitute
a recovery attestation.

With the GUC set exactly to `on`, any non-null timestamp is preserved, even one equal to `OLD.updated_at`.
Only `NEW.updated_at IS NULL` under this GUC fails atomically with `23502`. Without the GUC, or with a
value other than `on`, including `NULL`, the function forces `CURRENT_TIMESTAMP` and the UPDATE succeeds.

### Immutable historical function and triggers

Migration 001 established `public.update_updated_at()`; 039 does not rewrite it. Its canonical
identity is `pronargs = 0`, `prokind = 'f'`, `LANGUAGE plpgsql`, return `trigger`, `VOLATILE`,
`PARALLEL UNSAFE`, `SECURITY INVOKER`, and this body:

```sql
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
```

The upgrade/downgrade preflight and v4 compare attributes and the literal `prosrc` hash against this canon, in order
to detect a drifted `CREATE OR REPLACE` even at the same OID. The seven exact historical mappings
are:

| Table | Trigger | Function |
| --- | --- | --- |
| `decisions` | `trg_decisions_updated` | `public.update_updated_at()` |
| `learnings` | `trg_learnings_updated` | `public.update_updated_at()` |
| `snippets` | `trg_snippets_updated` | `public.update_updated_at()` |
| `runbooks` | `trg_runbooks_updated` | `public.update_updated_at()` |
| `adrs` | `trg_adrs_updated` | `public.update_updated_at()` |
| `features` | `set_features_updated_at` | `public.update_updated_at()` |
| `indexed_plans` | `set_indexed_plans_updated_at` | `public.update_updated_at()` |

Each row honors the exact trigger contract. The dedicated function is bound only to
`public.project_contexts` / `trg_project_contexts_updated`.

`RepairStore.apply_paths` and `RepairStore.rollback_before_finalize` run `SET LOCAL
brain_v42.allow_explicit_project_context_updated_at = 'on'` only after identity, head,
proof, and CAS are validated, immediately before an actual mutation. An `already_*` replay does not
enable it. Each `UPDATE` supplies `updated_at`, uses `RETURNING updated_at`, and compares the returned
value to the signed value before commit. Success, error, or rollback reset the GUC at the end of the
transaction. This marker coordinates the trigger; it authenticates nothing. Writers off, snapshot,
verified receipt, and attestations remain the mutation gates.

The GitNexus upstream impact is bounded to Maintenance/Unit, with no process affected:
`RepairStore.apply_paths` is MEDIUM (14 symbols, 12 direct) and
`RepairStore.rollback_before_finalize` is LOW (3 symbols, 1 direct). Any new HIGH or
CRITICAL analysis outside this surface blocks the implementation.

The function remains not `SECURITY DEFINER` and keeps the default PostgreSQL ACL `PUBLIC EXECUTE`.
A `RETURNS trigger` function is not callable like an ordinary SQL function. The preflight
requires `current_user = proowner(public.update_updated_at())`, an unchanged `nspowner(public)`, and no
default function ACL applicable to the active role, globally or within `public`; otherwise it fails before
DDL. `CREATE FUNCTION` therefore inherits this same owner, without `ALTER OWNER`.

The postcondition requires `proowner(new) = current_user = proowner(historical)`, `proacl IS NULL`,
and `COALESCE(proacl, acldefault('f', proowner)) = acldefault('f', proowner)`. The exploded ACL proves
exactly `EXECUTE` for owner and grantee `0`/PUBLIC. `has_function_privilege(current_user,
function_oid, 'EXECUTE')` completes the proof only for the migration role; it does not prove
PUBLIC. No `GRANT` or `REVOKE` is added.

## Migration 039, fail-closed

### Upgrade

Writers are stopped. In a single transaction, the migration first takes:

```sql
LOCK TABLE public.project_contexts IN ACCESS EXCLUSIVE MODE;
```

It first verifies the historical function against its migration 001 canon, attributes and `prosrc`
hash included. It then reads `pg_catalog` and requires exactly one trigger row with `tgrelid` of
`public.project_contexts`, `tgfoid` of the historical function, `tgtype = 19` (row + before +
update), `tgattr = ''::int2vector` (no `UPDATE OF`), `tgqual IS NULL` (no `WHEN`),
`tgparentid = 0`, `tgconstraint = 0`, `tgconstrrelid = 0`, `tgconstrindid = 0`,
`tgdeferrable = false`, `tginitdeferred = false`, `tgoldtable IS NULL`, `tgnewtable IS NULL`,
`tgenabled = 'O'`, `tgisinternal = false`, `tgnargs = 0`, and `tgargs = ''::bytea`. Any divergence
fails before DDL. This exhaustive list is the **exact trigger contract**; preflight, postflight,
downgrade, recovery v4, and tests apply it without reducing it.

After creating the dedicated function, it remaps the trigger with this exact qualified DDL:

```sql
CREATE OR REPLACE TRIGGER trg_project_contexts_updated
BEFORE UPDATE ON public.project_contexts
FOR EACH ROW EXECUTE FUNCTION public.set_project_context_updated_at()
```

It rereads exactly all these predicates with `tgfoid` of the dedicated function, then function, owner,
and ACL. Any error rolls back the transaction: Alembic version, function, and trigger remain unchanged.

### Downgrade

The downgrade fails without DDL by default. It explicitly requires:

```text
alembic -x allow_project_context_trigger_downgrade=yes downgrade 038
```

This option is allowed only after rollback and re-inventory, or after a full PostgreSQL
restore. With the opt-in, the migration takes the same lock before reading the catalog and verifies the
exact trigger contract, the historical function against its migration 001 canon, and the
dedicated function and its owner/ACL. It first reattaches the
trigger to the historical function, rereads this contract with its `tgfoid`, then executes
`DROP FUNCTION public.set_project_context_updated_at()` without `CASCADE`. Any error rolls back
version, function, and trigger to their initial state. The postflight after `DROP FUNCTION` requires the
exact historical trigger, all the trigger predicates above, and the absence of the
dedicated function.

## Recovery v4 and operator order

The bytes and digests of recovery v3 are immutable. v4 authority requires `head = 039`,
`schema_version = 4`, and 25 checks. The 25th is `project_context_updated_at_039`; it attests the
exact dedicated function, the canonical historical function, the GUC condition, the exact trigger, the
seven historical bindings `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`, `features`,
`indexed_plans`, and the attributes/ACL.
v4 and its tests compare this exact set, never a plain count of seven. No result
claims v4 before a `pg_restore` restore drill at 25/25. Recovery v4 and the migration tests
apply the same function/trigger predicate package as the 039 preflight and postflight.

The v4 assets are `brain-v42-v4.json`, `brain-v42-v4.sql`, and
`brain-v42-v4-pgrestore.sql`, with their tests. They complement v3 without modifying it.

The recovery order is strict: `pg_restore` of the production 037 backup in isolation, isolated
upgrade to Dream 038 then CAS 039, execution of `brain-v42-v4-pgrestore.sql`, then an isolated receipt of type
`brain-v42-v4-pgrestore` at 25/25. This isolated receipt authorizes the cutover, but does not yet prove
production. After writers off, the operator upgrades production to 038 then 039, runs
`brain-v42-v4.sql` live and obtains the authoritative live receipt of type `brain-v42-v4-live` at 25/25.
Only then does it launch inventory/repair, then reopen writers and restart the runtime.
`brain-v42-v4.sql` is therefore the live attestation, not a second restore. No live proof of
039 is claimed by this delivery. A 037 backup alone never proves 039. After a restore
post-finalize, the operator reapplies 038 then 039 and attests v4 live before writers or runtime; otherwise it
explicitly stays at 037 with the old runtime.

The head, store, runbook, spec, and plan move to 039. `apply_paths` returns
`already_applied` only if the seven contexts are already in the signed state. The rollback restores
exactly the paths and the non-null timestamp. After finalization, only a fully attested PostgreSQL
restore restores the data.

## Required tests

The proofs cover:

- static: function, trigger, owner, ACL, absence of `SECURITY DEFINER`, exact catalog, and
  literal SHA-256 `prosrc` hashes;
- upgrade from 037 without data mutation, greenfield, atomic downgrade without opt-in,
  opt-in downgrade, then re-upgrade;
- ordinary writer with an explicit timestamp without the GUC overwritten by the server clock;
- local opt-in that preserves any non-null timestamp, including one equal to the old one, only within apply
  and rollback, then a verified reset outside the transaction;
- GUC absent, `off`, `true`, `1`, or of different case, which forces `CURRENT_TIMESTAMP`;
- `NULL` under GUC `on` rejected atomically with `23502`, then `NULL` without the GUC or with an invalid GUC
  overwritten by `CURRENT_TIMESTAMP` with a successful UPDATE;
- exact invariance of `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`, `features`, and
  `indexed_plans`;
- canonical dedicated function, canonical migration 001 historical function, exact `prosrc` digests,
  and the eight exact table/trigger/function mappings;
- `apply_paths` replay as `already_applied`, exact rollback, and CAS/finalize without drift;
- migrations 038 then 039, Alembic chain in `tests/unit/test_alembic_env.py`, recovery v4 in
  `tests/unit/test_recovery_contract_v4.py` and
  `tests/unit/test_recovery_contract_v4_pgrestore.py`, and the repair 037→038→039 suites;
- `BEFORE UPDATE OF` drift that fails and fully rolls back upgrade or downgrade;
- exact `RETURNING updated_at`, absence of the GUC on `already_*` replay, and reset after a
  rollback error in `tests/unit/test_plan_index_repair_store.py`;
- Task 7: five out of five PostgreSQL proofs in a disposable container.

The tests re-run the PostgreSQL 16 capture in a uniquely named database or container,
accept no production URL, and delete the resource after capture. The two v4 receipts,
isolated `brain-v42-v4-pgrestore` then live `brain-v42-v4-live`, are tested as distinct
conditions.

The migration tests use an isolated database and capture the full before/after values.

## Deployment, rollback, and risks

The operator follows the recovery order above, keeps writers off until the attestations,
then runs inventory, `apply-paths`, project-by-project reindex, `verify`, and `finalize`.
`install.sh` precedes the restart, which remains the last action.

| Risk | Mitigation |
| --- | --- |
| Unexpected trigger or ACL | Lock, `pg_catalog` preflight, and post-DDL verification. |
| Function drift at the same OID | Exact attributes and SHA-256 hash of `prosrc`. |
| Ordinary writer injecting a timestamp or `NULL` | GUC absent or invalid: `CURRENT_TIMESTAMP` is forced. |
| Lost repair timestamp | Local GUC that preserves any non-null timestamp in apply/rollback and exact CAS. |
| Cutover based on the isolated receipt alone | Live receipt `brain-v42-v4-live` at 25/25 mandatory before repair. |

## Planned files

- Migration: `alembic/versions/039_project_context_timestamp_cas.py`.
- Store: `src/brain_v42/maintenance/plan_index_repair_store.py` and
  `src/brain_v42/maintenance/plan_index_repair.py`.
- Repair tests: `tests/unit/test_plan_index_repair.py`,
  `tests/unit/test_plan_index_repair_store.py`, `tests/unit/test_repair_plan_index_cli.py`, and
  `tests/integration/test_plan_index_repair.py`.
- Migration/integration tests: `tests/unit/db/test_migration_039_project_context_timestamp.py`,
  `tests/integration/db/test_migration_039_project_context_timestamp.py`, and
  `tests/unit/test_alembic_env.py`.
- CLI and repair documents: `scripts/repair_plan_index.py`,
  `docs/PLAN_INDEX_REPAIR_RUNBOOK.md`, this specification, the repair specification, and its plan.
- Repository and production documentation: `README.md`, `CLAUDE.md`, the runtime `docs/`
  documents, and the operator runbook.
- Recovery v4: `ops/recovery/brain-v42-v4.json`, `ops/recovery/brain-v42-v4.sql`,
  `ops/recovery/brain-v42-v4-pgrestore.sql`, `tests/unit/test_recovery_contract_v4.py`, and
  `tests/unit/test_recovery_contract_v4_pgrestore.py`.

The historical 037 and v3 artifacts do not change.

## Acceptance criteria

1. The trigger keeps the ordinary auto-timestamp and preserves any non-null explicit timestamp,
   even one equal to the old one, only with the local repair GUC; only `NULL` under GUC `on` fails
   atomically, and `NULL` without the GUC is auto-stamped.
2. Apply, replay, rollback, and finalize preserve an exact CAS without timestamp drift.
3. Upgrade and downgrade lock, verify the exact catalog, and fully roll back on
   error.
4. `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`, `features`, `indexed_plans`, the
   037 assets, and recovery v3 remain unchanged, with their canonical historical mappings.
5. v4 is declared only after the 25 checks, including `project_context_updated_at_039`, and the
   isolated drill `brain-v42-v4-pgrestore` at 25/25, followed by the live receipt `brain-v42-v4-live` at 25/25 before repair.
