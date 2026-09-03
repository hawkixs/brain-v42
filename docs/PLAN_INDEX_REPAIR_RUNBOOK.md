# Plan index repair runbook

This runbook repairs the seven project indexes named in ticket
`44ee7643-fb06-4186-a364-cb175610b973`. It is an operator procedure for a separately authorized
production window. This code-delivery mission does not migrate, deploy, restart, reindex, apply,
finalize, roll back, or restore production.

Run one gate at a time, in the order below. Stop at the first failed or ambiguous check. Keep all
writers off until a step explicitly permits the isolated MCP process. Never add `--wet`, `--force`,
`--skip-backup`, or a combined apply/finalize command; the repair CLI exposes none of them.

## Current targets

Everything normative in this runbook points at this block. It is the **only** place in the file
where an Alembic head, a recovery-contract asset, or a receipt denominator is written as a
literal; the dated sections below are records of past cutovers and keep the numbers of their own
day. `tests/unit/test_runbook_normative_values_have_one_source.py` enforces both halves of that
rule, and it fails on the version of this file that shipped before 2026-08-22 — the day the gates
here and the restore procedure below were found to be describing two different databases.

<!-- dr-current:start -->
| Target | Value | Measured, and against what |
| --- | --- | --- |
| Alembic head | `052` | 2026-09-03 11:20 CEST, live production, measured right after the 051 → 052 upgrade |
| Recovery contract, live target | `ops/recovery/brain-v42-v9.sql` | **30/30 measured on production at head `052`**, 2026-09-03 11:27 CEST, read-only replay. Receipt: `ops/recovery/receipts/2026-09-03-live-v9-052.md`. The mint predicted this from 22/30 on a disposable database and named the eight-check gap in advance (six data checks on an empty database, the server's vector build, 050's disabled trigger); production has data and the trigger armed, so all eight close. |
| Recovery contract, previous generation | `ops/recovery/brain-v42-v8.sql` | Frozen. Its own 30/30 at head `051` on 2026-09-03 stands and is not rewritten — v8 attested the state v8 described. Superseded as the replay target by v9 at 11:27 the same day. |
| Recovery contract, restored target | `ops/recovery/brain-v42-v8-pgrestore.sql` (v9 twin minted, unreplayed) | **Replayed against a real restore on 2026-09-03**, receipt `ops/recovery/receipts/2026-09-03-p1-restore-051.md`, ticket `58711012` served. Source: `/data/backups/20260903_010223/brain-v42.dump.gz` (rail `red-backup`, 03:02, sha256 `c7baadce…` equal to its manifest), head `051` read INSIDE the dump with `pg_restore --data-only --table=alembic_version` before restoring. The sentence this row carried until 00:11 that day — "no dump on hand can do it" — was true when written and false two hours fifty-one minutes later, when the nightly rail wrote that dump; nothing marked it perishable. Also DERIVED and replayed against a fresh chain-built 051 database by `tests/integration/db/test_fresh_head_is_the_yardstick.py` |
| Contract receipt, live asset replayed live | `30/30` — zero failing checks, out of 30 checks total (050 and 051 add no CHECK to the receipt: they extend existing invariants, they do not create new ones). The v7 receipt read **24/30** at head 051 before this mint, its six reds being `table_set`, `table_shape`, `catalog_counts`, `sequence_shape`, `trigger_function_fingerprints` and `brain_runtime_032_036_037` — every one of them a consequence of 050/051 | 2026-09-03, live production |
| Contract receipt, `-pgrestore` asset against a real restore | `30/30` — zero failing checks out of 30, at head `051`. `sequence_shape`, the one check a LIVE replay cannot exercise, passes here. `extension_versions` passes while REPORTING a drift (`origin_inventory` `vector 0.8.2` against observed `0.8.5`): since v6 the check is `extension_inventory`, `restore_rule: names-only`, so the drift is visible without gating. Replay: `psql -U brain -d brain -Atq -v ON_ERROR_STOP=1 -f ops/recovery/brain-v42-v8-pgrestore.sql` on the restored database | 2026-09-03, disposable container `brain_drill_20260903` (image `sha256:b295c2aa9272`), destroyed after; receipt `ops/recovery/receipts/2026-09-03-p1-restore-051.md` |
| ACL contract, live target | `ops/recovery/brain-v42-v9-acl.sql` | `1/1` on production at head `052`, 2026-09-03 11:27 CEST. The first replay returned `contract_id v9-acl` next to `schema_version 8` — a gap the mint left in the `.sql` form and its own unit test could not see, having been written from the mint script; corrected and re-replayed. |
| ACL contract, restored target | `ops/recovery/brain-v42-v8-acl-pgrestore.sql` | Replayed on 2026-09-03 against the same real restore. Still byte-identical to the v7 twin but for its identity, because 050 and 051 grant nothing and touch no view — a premise re-derived from EVERY migration file, not from named ones, and now also OBSERVED: the replay reports zero owner and zero grant mismatches at head 051 |
| ACL receipt, live | `1/1` — 0 owner, 0 grant, 0 role-privilege and 0 unexpected-grantee mismatches | 2026-09-03, live production |
| ACL receipt, restored | `1/1` — 0 owner, 0 grant, 0 role-privilege and 0 unexpected-grantee mismatches, naming its one tolerance `tolerated_superuser_roles: ["postgres"]`, the maintenance superuser that performed the restore. The restore ran WITHOUT `--no-owner --no-acl`, which is what makes this row mean anything. Replay: `psql … -f ops/recovery/brain-v42-v8-acl-pgrestore.sql` | 2026-09-03, same disposable restore as the row above; receipt `ops/recovery/receipts/2026-09-03-p1-restore-051.md` |
| Search top-10 churn across HNSW rebuilds, `learnings` ONLY, n=40 probes | `0` — overlap `10/10` on ten build pairs (`BUILDS=5`, seed 0.42), strict order included, both probe bands | 2026-08-29, copy of the 3243 real `learnings` embeddings, index path forced |

Replay the head and the two live-target assets against production:

```bash
docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select version_num from alembic_version;"
for asset in brain-v42-v8.sql brain-v42-v8-acl.sql; do
  docker exec -i brain_v42_postgres psql -U brain -d brain -Atq -v ON_ERROR_STOP=1 -f - \
    < "ops/recovery/$asset"
done
```

**The two `-pgrestore` twins are deliberately absent from that loop.** Until 2026-08-28 this
document looped over every asset against `-d brain`, so the assets written to attest a
*restoration* were replayed against the original. They pass there — their fingerprints were
minted on that very database — so no index residue, no `extversion` drift and no
`pg_dump`/`pg_restore` round-trip divergence can ever surface in the replay this runbook
prescribes. Run them only against a genuinely restored target, and say which one:

```bash
# The restored target lives in its OWN PostgreSQL instance (its own container),
# never inside the production cluster: a database created next to `brain` in
# `brain_v42_postgres` shares its binaries, so the extversion drift this asset
# exists to surface is invisible by construction. `pg_restore --clean --create`
# recreates the database under its archived name — `brain` — and that name is
# EXPECTED here, outside production; what must never happen is replaying this
# against the production cluster itself.
RESTORED_CONTAINER=${RESTORED_CONTAINER:?name the container holding the restored instance}
[ "$RESTORED_CONTAINER" != "brain_v42_postgres" ] || { echo "refusing: that is the production cluster" >&2; exit 2; }
for twin in brain-v42-v8-pgrestore.sql brain-v42-v8-acl-pgrestore.sql; do
  docker exec -i "$RESTORED_CONTAINER" psql -U brain -d "${RESTORED_DB:-brain}" -Atq -v ON_ERROR_STOP=1 -f - \
    < "ops/recovery/$twin"
done
```

**The extension check on the restored target is names-only, and its receipt says so.** The v5
twin pinned `vector 0.8.2` — the version production *declares* — while every image this
repository can restore into reports `0.8.4` or `0.8.5`, so a perfectly healthy restore failed
that one check (the flaw ticket `2ed0d4e0` named). The twin requires the extension NAMES
only — the rule was introduced by v6 and v8 still inherits it byte for byte — and its receipt states
both versions instead of judging them: measured 2026-08-29 on the v6 twin, the
`extension_versions` check **passes** while printing `origin_inventory: plpgsql 1.0, vector
0.8.2` against `inventory: plpgsql 1.0, vector 0.8.5`. Do not read that version line as a
failure — a healthy `0.8.2 → 0.8.5` restore is exactly what it looks like. The strict version
pin still lives where it can bite honestly: the live asset pins the full inventory production
declares, so a version moving under production without a re-mint reddens *there*. A twin
receipt short on any check is a failed restore, with no named exception left.

**What the v8 mint measured, and what it did not.** Minted 2026-09-03 at head 051 to carry
050 and 051 jointly. Measured: the live receipt at `30/30`; the live ACL receipt at `1/1`; a
negative-mutation matrix of thirteen rows, each corrupting ONE of the new pins in the ASSET
(never in the database) and each dropping the receipt to `29/30`, so no new pin is vacuous;
and a diff against the frozen v7 bytes showing the delta is purely ADDITIVE — the only removed
lines are the two catalog counters and the identity, so no expected value of v7 was re-signed.
NOT measured: any replay against a real restore, for either twin. One more thing is measured
and worth carrying: a database freshly built by `alembic upgrade head` does **not** reproduce
production here, by exactly one trigger. 050 ships
`project_contexts_focus_history_required` DISABLED at birth and arming it is a separate
operator gesture (performed 2026-09-02 at 23:54:27), so production reads `tgenabled = 'O'` and
a chain-built database reads `'D'`. The contract demands the ARMED form on purpose; the
yardstick pins the gap at its exact value rather than absorbing it.

**Read "variants" as two FILES, never as two targets.** There is one database in the loop above
and two assets; the word has already been read the other way once, and that misreading is what
kept the restored-target row empty for six days without anyone noticing.

**On the churn row.** The row is measured on `learnings` **only** — the table the reference
measurement already found the most stable — with n=40 probes in two distance bands: 20
near-duplicates (1-NN distance 0.024–0.026) and 20 at realistic query distance (1-NN
0.282–0.325 — the band the bench prints on every run, which is the source). Five rebuilds,
ten build pairs, seed 0.42; the single recall miss against exact search sat at
`Δd = 0.000000` — a pure tie shuffle, zero semantic loss. Do **not** generalize this `0` to
every HNSW index. The reference is `docs/runbooks/2026-08-23-hnsw-restore-churn-declaration.md`
(n=1544 queries, all 9 tables, the same real embeddings): it puts duplicate-heavy tables far
outside this row — `gitlab_events` at 8.85/10 with 100 % of the divergence being tie
shuffling — and it, not this row, carries the operator rule for reading a restore: **never
compare identifier lists**; compare the number of rows returned and the mean top-10 distance
(its §3 probe, with measured healthy and broken bands). Identifiers that move after a restore
at unchanged distances are the documented noise of tie ordering, not a corruption signal.
Two more things this row does not say. First, the bench forces the index path
(`set enable_seqscan=off`) while production currently seq-scans `learnings`: the declaration's
§0 measures the switch-over threshold at roughly 5,800 `learnings` rows and gives the
one-query `idx_scan` control to replay before trusting any HNSW statement, this row included.
Second, the bench runs the *versioned* image, not the live runtime — the two have drifted, and
`pgvector` reports three different versions across the running container, the pinned target
and what production actually has installed. Ticket `2ed0d4e0` carries that; do not read the
churn row as evidence about the running server's exact build. Re-run
`scripts/hnsw_churn_measure.sh` after any notable corpus growth — it prints its provenance,
seed and probe bands, refuses an empty or foreign bench, and destroys its bench on exit — and
replay the §0 control once `learnings` approaches the seq-scan threshold.
<!-- dr-current:end -->

**Replay it, do not quote it.** The receipt denominator moved four times on 2026-08-22 alone, and
the head has advanced nine times since the procedure below was first written. Every value above is
dated for that reason: a number without its date is a trap, and this document has already sprung
it once.

**What the restored-side rows do and do not prove.** For the first time the table above carries
receipts measured against a `pg_restore`d target, not only against live production — the third
column says which side each row was measured on, so this is checkable rather than remembered.
The restored rows come from a disposable bench: a same-day production dump, restored by a
maintenance superuser with the disaster procedure's own flags (`--exit-on-error --clean
--create`), both twins replayed, the bench destroyed. That closes the mechanism `5dc286b6` named — this document now
prescribes commands that produce receipts against a restored target — and it exercises the one
control written FOR a restore that a live replay never could: on a live database
`last_value >= max(id)` is true by construction, and only the restored side makes it bite.
Owners and grants are no longer unattested on a restored target either — the disaster-path
restore preserves them (measured: 0 owner, 0 grant, 0 role-privilege mismatches), and the ACL
twin attests it. What a full bench receipt still is NOT: the operator recovery procedure —
roles left `NOLOGIN`, canaries, watchdog re-arm — has never been rehearsed end to end. Do not
read the rows above as "DR is proven"; the P1 gate lives in `58711012` (which inherited it when
`8eaefe36` closed on 2026-08-28 as superseded by five CATALOGUE splits, none carrying the
gate), and only that ticket, not this table, says when it closes.

The churn row is the one line above that does **not** come from production: it is measured on a
read-only copy of the real corpus, and it describes index rebuilds rather than a restoration.
It belongs here because an operator reads it at the same moment as the receipts — but it is not
evidence about a restore, and it must not be counted as one.

**Nothing here fails when these values go stale.** `test_runbook_normative_values_have_one_source.py`
governs *where* a current value may be written; it never asks whether the value is still true.
The head row above sat two revisions behind the deployed database for six days and the suite
stayed green throughout. That gap is tracked separately — treat every date in the third column as
the age of a claim, not as a guarantee.

## Fixed scope and private evidence

The repair accepts exactly these projects:

- `red-games`
- `red-gift`
- `red-phone`
- `red-quant`
- `red-shrik`
- `red-viewer`
- `red-writer`

Run commands from the deployed repository root. Create a private evidence directory on durable,
operator-controlled storage. Do not put credentials, DSNs, environment dumps, plan contents, or
embeddings in any evidence file.

```bash
set -euo pipefail
REPO_ROOT=/ABSOLUTE/PATH/TO/brain_v42
EVIDENCE_DIR=/ABSOLUTE/PRIVATE/PATH/plan-index-repair
cd "$REPO_ROOT"
install -d -m 0700 "$EVIDENCE_DIR"

MANIFEST="$REPO_ROOT/ops/recovery/plan-index-repair-v1.json"
SNAPSHOT="$EVIDENCE_DIR/control-snapshot.json"
BACKUP_RECEIPT="$EVIDENCE_DIR/postgres-backup-receipt.json"
REINDEX_EVIDENCE="$EVIDENCE_DIR/reindex-evidence-v1.json"
VERIFICATION_REPORT="$EVIDENCE_DIR/verification-report.json"
RUN_ID="$(uv run python -c 'from uuid import uuid4; print(uuid4())')"
readonly REPO_ROOT EVIDENCE_DIR MANIFEST SNAPSHOT BACKUP_RECEIPT \
  REINDEX_EVIDENCE VERIFICATION_REPORT RUN_ID
```

Every control file must be a regular file owned by the operator with mode `0600`. The CLI refuses
changed digests, another owner, another mode, symlinks, and reused output paths. Preserve the
snapshot, backup, receipt, evidence, report, command outputs, and their SHA-256 digests together.

<!-- project-context-cas-039:start -->
## Project-context timestamp CAS (039)

Repository target: 039. Executed on 2026-08-03; production measured at `039` on 2026-08-04.
Revision 038 adds Dream ticket-extraction state; revision 039 adds the project-context timestamp
contract. The order below is the record of that cutover — read it as history, and re-measure the
deployed head before relying on any statement here.

Use this single operator order:

1. **backup production 037** with the approved complete custom-format backup procedure.
2. **pg_restore isolated** into a PostgreSQL 16 target with no production routing.
3. **upgrade isolated 038 then 039** and verify `alembic current` reports `039 (head)`.
4. Run `brain-v42-v4-pgrestore.sql` against the restored target and retain the **isolated v4
   receipt: 25/25**.
5. Hold **writers off** in production and verify the MCP watchdog timer and service are quiescent.
6. **upgrade production 038 then 039** in one authorized, writers-off migration window.
7. Run `brain-v42-v4.sql` against production and retain the **live v4 receipt: 25/25**.
8. Run `inventory` and approve the signed seven-project snapshot.
9. Run `apply-paths` with the unchanged backup receipt and writers-off proofs.
10. **reindex one project at a time**, preserving the exact result of every call.
11. Run `verify` and validate its private report and digest.

> **Which contract to run, as of 2026-08-21.** The eleven steps above are the sequence of the
> **038→039** rollout and are kept verbatim — they describe a migration that happened, and
> rewriting their steps would falsify the record. For any run **today**, substitute the current
> contract: **`brain-v42-v5.sql`** and **`brain-v42-v5-pgrestore.sql`**. The `v4` assets pin
> `alembic_head = 039` and therefore cannot return 25/25 past that head — not because the
> database degraded, but because the contract was minted against an older one. The v5 assets
> derive the head (signature **S1**): the invariant is *exactly one applied head*.
> (Dated advice, and v5 was itself superseded on 2026-08-29: for any run **today**, substitute
> the contract generation named in [Current targets](#current-targets) — its restored-target
> twin is names-only on extension versions, where the v5 twin's strict pin false-reds every
> healthy restore.)
12. Run `finalize` only after all seven reindexes and verification succeed.
13. Run the non-publishing installer preflight and render verified MCP units into a new private
    directory outside systemd; inspect all three artifacts:

    `render_parent` is the private, pre-existing parent directory that contains `render_dir` and
    any temporary backup. `render_dir` is the new child directory holding generated unit files,
    so `/tmp/systemd-render.ABC123` is the parent and `/tmp/systemd-render.ABC123/units` is the
    render directory. The installer applies the parent ancestry guard before creating the child.

    ```bash
    set -euo pipefail
    TMPDIR="${TMPDIR:-/tmp}"
    render_parent="$(mktemp -d "$TMPDIR/systemd-render.XXXXXX")"
    render_dir="$render_parent/units"
    trap 'rm -rf -- "$render_parent"' EXIT
    chmod 0700 "$render_parent"
    test ! -e "$render_dir" && test ! -L "$render_dir"

    ./deploy/systemd/install.sh --check-only
    ./deploy/systemd/install.sh --render-dir "$render_dir"
    test -d "$render_dir" && test ! -L "$render_dir"
    for unit in \
      brain-mcp-http.service \
      brain-mcp-http-watchdog.service \
      brain-mcp-http-watchdog.timer; do
      test -f "$render_dir/$unit"
    done
    ```

14. Authorize the bounded live publication only after that inspection, then execute the
    [canonical MCP publication preflight](../deploy/systemd/MCP_HTTP_RUNBOOK.md#preflight)
    exactly as written. It repeats the non-publishing render, neutralizes the watchdog, backs up,
    atomically publishes only the three MCP units, reloads systemd, and verifies the live units.
15. Run `systemctl --user restart brain-mcp-http.service`.
16. Pass `curl --max-time 10` against the health endpoint.
17. Pass one **read-only MCP call**.
18. Run `systemctl --user enable --now brain-mcp-http-watchdog.timer` last.

Before inventory and throughout repair, prove these states exactly:

- `brain-mcp-http-watchdog.timer is inactive and disabled`;
- `brain-mcp-http-watchdog.service is inactive`;
- `brain-mcp-http.service is inactive and MainPID=0` except during the bounded seven-call
  maintenance reindex window.

Stop the maintenance MCP again before `verify` and `finalize`. A partial reindex is restore-only:
the seven MCP calls commit independently, so a failed, timed-out, malformed, or nonzero-error
result after any call forbids both bounded rollback and finalization. Keep writers and MCP stopped
and restore the complete tested backup.

The two recovery attestations are separate operator gates, not repair receipts. Store the isolated
result as `brain-v42-v4-pgrestore-result.json` with
`brain-v42-v4-pgrestore-provenance.json`. Store the live result as
`brain-v42-v4-live-result.json` with `brain-v42-v4-live-provenance.json`. Publish each result and
its provenance together as private regular files in mode 0600. Each result must contain exactly
25 unique checks, the IDs from `brain-v42-v4.json`, and all statuses are pass. Bind each provenance
record to the code commit, frozen SQL and JSON hashes, result hash, backup/archive hash, and target
fingerprint. MutationProof and the backup receipt remain unchanged.

Before `finalize`, `rollback-before-finalize` accepts only `rolled_back` with the signed affected
row count or the exact idempotent `already_rolled_back` result. Re-inventory and compare the full
context paths, signed timestamps, indexed plans, feature links, polluted IDs, missing canonical
files, and database identity with the original snapshot. After finalize, restore the complete
tested backup; in-place rollback cannot reconstruct deleted plan contents or embeddings. Keep
writers off, restore head 037, reapply 038 then 039, and obtain a new live 25/25 v4 receipt before
starting the revision-039 runtime. Alternatively, remain at 037 with the revision-037 runtime.
<!-- project-context-cas-039:end -->

<!-- project-context-focus-updated-at-040:start -->
## Focus timestamp (040)

Repository target: 040. This section claims no live head; measure it. Applied to production on
2026-08-04 in a writers-off window; `alembic_version` then measured `040`, all 52 rows still
read NULL, and the 039 function digest was unchanged at `60c6154d…` / 391 octets.

Revision 040 adds `project_contexts.focus_updated_at`: one nullable `TIMESTAMPTZ`, no server
default, no backfill, no trigger change. It is additive and does not alter the catalog contract
that revisions 036–039 pin, so it does not need the isolated-restore ceremony those revisions
required. It is not free of consequence, though — apply it and the code together:

- `_REQUIRED_ALEMBIC_HEAD` in `plan_index_repair_store.py` moves to `040`. Between merging the
  code and stamping production, the plan-index repair tool is fail-closed and refuses to run.
  That window is the reason 040 ships and deploys in the same operator session.
- ~~`ops/recovery/brain-v42-v4.*` still pins `alembic_head = 039`, so the live v4 attestation reads
  **24/25** until the v5 assets exist. Expect it; do not read it as corruption.~~

  > **AMENDED 2026-08-21 — the v5 assets now exist, and the number above was wrong.**
  > The live **v4** receipt was measured at **22/25**, not 24/25, on 2026-08-20 against head
  > `045`. Its three failures were all consequences of migrations applied *after* v4 was
  > minted at `039` — `alembic_head` 039≠045, `catalog_counts.indexes` 128≠129
  > (`idx_dream_runs_date_project`, migration 042), and the `codex_dream_run_v1` column
  > fingerprint (migration 045 widened `dream_runs.model`). **None of them was corruption**,
  > which is precisely why a contract that pins a revision stops teaching anyone anything.
  >
  > **`ops/recovery/brain-v42-v5.*` is now the current contract**, and it derives the head
  > (signature **S1**, decision `9d22bc6a`): the invariant is *exactly one applied head*, not
  > *the head equals N*. The exact revision stays proven, fail-closed, by
  > `_REQUIRED_ALEMBIC_HEAD` on the code side. **Measured 2026-08-21 against production at
  > head `046`: 25/25**, both variants — `brain-v42-v5.sql` and `brain-v42-v5-pgrestore.sql`.
  > (v5 was itself superseded on 2026-08-29; the current generation and its receipts live in
  > [Current targets](#current-targets).)
  >
  > **Replay it, do not quote it.** Every receipt in this document is dated, and a receipt
  > without its date is a trap: this very line carried `24/25` for long enough to be believed.
  >
  > **EXTENDED 2026-08-22 — the denominator is now 26, and 25/26 is a FAILURE, not a stale gate.**
  > Ticket `81c4f366` added `inherited_constraint_definitions`: the constraints inherited from
  > migrations 033/034/035 and from `projects` were attested by **name** only, so a constraint
  > whose definition drifted while keeping its name passed. Twenty-nine constraints across
  > `brain_entities`, `entity_relations`, `graph_outbox`, `graph_projection_leases` and
  > `projects` are now pinned by `md5(pg_get_constraintdef(...))`, in both directions
  > (missing-or-divergent, and present-but-unlisted). Everything above stays true — the head
  > is still derived, `_REQUIRED_ALEMBIC_HEAD` still carries the exact revision.
  > **Measured 2026-08-22 against production at head `046`: 26/26**, both variants.
  >
  > **EXTENDED again, same day — the denominator is 27. `26/27` is a FAILURE.**
  > Ticket `2bb1988f`, the fifth split, closed the pan its predecessor left open:
  > `81c4f366` was bounded to *constraints*, so the **17 indexes, 58 columns and 5 relation
  > shapes** of those same five historical tables were attested by nothing — not even their
  > existence as ordinary, non-partitioned heap tables. `historical_relation_shape` now pins
  > index definitions (`md5(pg_get_indexdef(...))`, bidirectional), per-table column
  > fingerprints, and the nine-property relation template already applied to `brain_sessions`.
  > Its observed value names its three counters, so a failure says which one moved.
  > **Measured 2026-08-22 against production at head `046`: 27/27**, both variants.
  >
  > **EXTENDED a third time, same day — the denominator is 28. `27/28` is a FAILURE.**
  > Ticket `f36846a1`: the **nine sequences** were attested by nothing at all — `grep -ci
  > sequence` on the previous asset returned `0`. `sequence_shape` now pins their shape
  > (owning table and column, type, increment, bounds, `CYCLE`) in both directions, and —
  > the control that actually matters — `last_value >= max(id)` on each owning column.
  > That second one is the silent restore failure: a restore that skips the `setval`s
  > leaves every sequence at 1, the catalogue is complete, every `SELECT` passes, and the
  > FIRST `INSERT` dies on a primary-key collision. Its observed value names its two
  > counters. Everything above stays true.
  > **Measured 2026-08-22 against production at head `046`: 28/28**, both variants.
  >
  > **EXTENDED a fourth time, same day — the denominator is 29. `28/29` is a FAILURE.**
  > Ticket `75112bc6`, the last of the five splits. Production runs **14 distinct trigger
  > functions**; exactly **one** was fingerprinted (`update_updated_at`, by the 039
  > invariant). The other thirteen — including the two that stamp `content_updated_at`
  > (041) and `freshness_status` (043) — could be rewritten without a single byte of the
  > contract moving. `trigger_function_fingerprints` now pins all fourteen by
  > `sha256(prosrc)` and octet length, bidirectionally, and bounds the observed set by the
  > attributes a trigger function must keep — so one that gains `SECURITY DEFINER` drops
  > OUT of the observation and reads as missing, which is the truth. It also names the
  > **11 stamping triggers** the asset did not name, **with their `WHEN` clause**: all
  > eleven are conditional, and one recreated without its clause would stamp on every
  > write while keeping its name, its table and its function.
  > **Before fingerprinting, the drift was audited** as the parent ticket demanded — a
  > database built fresh by `alembic upgrade head`, compared to production: **zero
  > divergence**. Without that, the fingerprint would have engraved a drift as reference.
  > **Measured 2026-08-22 against production at head `046`: 29/29**, both variants.
  >
  > **A SECOND, SEPARATE receipt — `ops/recovery/brain-v42-v5-acl.sql`, and it is NOT part
  > of the 28.** Ticket `60708007`. The sandbox restore runs `--no-owner --no-acl`, so a
  > restoration receipt erases the very thing this proof looks at. Owners and grants
  > therefore live in their own asset, played against **live production only**, and they
  > have **no `-pgrestore` twin** — that absence is the decision, not an oversight. It pins
  > that every one of the 51 public relations is owned by `brain`, that `codex_ro` holds
  > `SELECT` on exactly the ten codex views the migration 036 grants (and `USAGE` on the
  > schema, without which those ten grants are inert), and that `codex_ro` never gains a
  > role attribute or a role membership. **Measured 2026-08-22 against production at head
  > `046`: 1/1.** Run it with the same command, substituting the file:
  > `docker exec -i brain_v42_postgres psql -U brain -d brain -Atq -v ON_ERROR_STOP=1 -f -
  > < ops/recovery/brain-v42-v5-acl.sql`
  >
  > **AMENDED 2026-08-29 — the derogation above was measured too wide, and the twin now
  > exists.** The `--no-owner --no-acl` erasure is a property of the SANDBOX rehearsal
  > only; it was never a property of the disaster path, which restores with
  > `--exit-on-error --clean --create` under the maintenance superuser. A real restore
  > measured that day preserved owners and grants intact — 0 owner, 0 grant,
  > 0 role-privilege mismatches — so "a restoration receipt erases the very thing this
  > proof looks at" was false for the path that matters. The current ACL contract
  > therefore ships a `-pgrestore` twin (named in [Current targets](#current-targets)),
  > whose receipt names its one tolerance: `tolerated_superuser_roles: ["postgres"]`, the
  > maintenance superuser that performed the restore. The paragraphs above and below stay
  > as the record of the v5-era belief; the caveat below was true of the v5 numbers, and
  > the current targets table now carries restored-side receipts.
  >
  > **What no receipt here proves.** Every number above was replayed against the **live**
  > production database, never against a `pg_restore`d dump. None of them says anything about
  > an actual restoration. The P1 gate — first raised as `8eaefe36`, carried by `58711012`
  > since its parent closed on 2026-08-28 — stands entirely open — do not read
  > `29/29` as "DR is proven". The sequence check makes that sharper, not softer: on a live
  > database `last_value >= max(id)` is true by construction, so the one control written
  > FOR a restore is the one control no receipt here can ever exercise.

Operator order:

1. **backup production** with the approved complete custom-format backup procedure.
2. Verify the deployed head is exactly `039`:
   `docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"`
3. **upgrade production 039 then 040** in one writers-off window.
4. Re-measure `alembic_version` and confirm `040`, and confirm the column exists and is nullable:
   `select is_nullable, column_default from information_schema.columns
   where table_name='project_contexts' and column_name='focus_updated_at';`
5. Confirm the backfill did not happen — every pre-existing row must still read NULL:
   `select count(*) from project_contexts where focus_updated_at is not null;` must return `0`
   before any focus is written.
6. Restart the MCP service last, then canary `brain_session_start` and check the briefing renders
   `Focus écrit : inconnu (jamais horodaté)` — NULL rendering as "unknown" is the proof that no
   number was invented for prose nobody dated.

Rollback is `alembic downgrade 040:039`, which drops the column. It needs no opt-in flag: 040
installs no function and no trigger, so nothing signed by 039 is at risk. The only loss is the
focus timestamps written since the upgrade, which are re-derivable only by writing a focus again.
<!-- project-context-focus-updated-at-040:end -->

## 1. Confirm the deployed head matches the repair tool

There is no migration left to gate here. The repair CLI is fail-closed on
`_REQUIRED_ALEMBIC_HEAD` in `src/brain_v42/maintenance/plan_index_repair_store.py`: `inventory`
records the head it read from `alembic_version`, and `apply-paths` and `finalize` each refuse
unless that recorded head equals the constant **and** equals the head they re-read inside their
own transaction. So this step proves one equality, and both sides of it are measured, never
quoted — a hard-coded gate goes stale at the next migration and, worse, tells an operator that
today's head is an anomaly.

```bash
uv run alembic history
BRAIN_ALEMBIC_ALLOW_PROD=1 uv run alembic current
ALEMBIC_HEAD="$(docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  'select version_num from alembic_version;')"
readonly ALEMBIC_HEAD
grep -n '^_REQUIRED_ALEMBIC_HEAD' src/brain_v42/maintenance/plan_index_repair_store.py
```

The history must show one linear head. `$ALEMBIC_HEAD` must equal both that head and
`_REQUIRED_ALEMBIC_HEAD`. Stop on any other value: the repair refuses to run anyway, and learning
that here costs less than learning it after the backup. Compare `$ALEMBIC_HEAD` with
[Current targets](#current-targets) and re-date that block if they differ — a divergence there is
a stale document, not a stale database. Keep every production writer off before continuing.

## 2. Stop every writer

Stop and keep stopped:

- MCP HTTP, its watchdog, and every MCP stdio client;
- Dream, automation, scheduled jobs, and any legacy automation writer;
- ad hoc index, migration, maintenance, and database scripts;
- every external process that can write Brain PostgreSQL.

Record the exact units and processes stopped on the target host. Disable or stop timers before
their services so they cannot restart a writer. Verify quiescence from both the process manager and
`pg_stat_activity`; an attestation flag does not perform this check. Do not proceed while an
unidentified write transaction, prepared transaction, or restart policy remains active.

Keep the MCP watchdog off for the entire window. Later, start only the isolated MCP service long
enough to run the seven reindexes, then stop it again before verification and finalization.

## 3. Take a full backup and prove its restore

Take a complete PostgreSQL backup with the approved production backup procedure. Restore that
exact backup into an isolated target with no production routing, then verify:

- restore completion without ignored errors;
- the Alembic head measured in step 1 as `$ALEMBIC_HEAD`;
- the expected schema, constraints, indexes, extensions, and table counts;
- the recovery contract named in [Current targets](#current-targets), `-pgrestore` variant, run
  against the restored target and returning its full receipt. A short receipt is a failed
  restore, not a stale gate. The former named exception is gone: since the current contract
  generation, the extension check on the restored target is names-only and a healthy
  version drift is stated in the receipt instead of failing it (see the names-only note in
  [Current targets](#current-targets)).

The restore test must use the same backup later designated for full recovery. A control snapshot
is not a database backup: it omits plan contents, embeddings, and unrelated rows.

Write a secret-free receipt that identifies the backup and successful restore test. The following
shape is illustrative; use the real immutable backup and restore identifiers:

```bash
umask 077
printf '{"version":1,"backup_id":"REPLACE","restore_test_id":"REPLACE","alembic_head":"%s","status":"passed"}\n' \
  "$ALEMBIC_HEAD" > "$BACKUP_RECEIPT"
chmod 0600 "$BACKUP_RECEIPT"
BACKUP_RECEIPT_SHA256="$(sha256sum -- "$BACKUP_RECEIPT" | cut -d ' ' -f 1)"
readonly BACKUP_RECEIPT_SHA256
```

Stop if the archive is incomplete, the restore target was not isolated, the restore was not
tested, or the receipt cannot be tied to the tested backup.

## 4. Inventory and approve the exact seven-project snapshot

Inventory is read-only for PostgreSQL and writes one private local snapshot. The snapshot path
must not exist before the command.

```bash
test ! -e "$SNAPSHOT" && test ! -L "$SNAPSHOT"
uv run python scripts/repair_plan_index.py inventory \
  --run-id "$RUN_ID" \
  --manifest "$MANIFEST" \
  --snapshot-output "$SNAPSHOT"
test "$(stat -c '%a' -- "$SNAPSHOT")" = 600
SNAPSHOT_SHA256="$(sha256sum -- "$SNAPSHOT" | cut -d ' ' -f 1)"
readonly SNAPSHOT_SHA256
```

The command must return `status="snapshotted"` and `project_count=7`. Inspect counts without
printing private paths or row identities:

```bash
jq '{
  version,
  alembic_revision,
  project_count: (.contexts | length),
  local_file_count: (.local_files | length),
  indexed_plan_count: (.indexed_plans | length),
  polluted_plan_count: (.polluted_plan_ids | length),
  missing_canonical_count: (.missing_canonical_files | length),
  collision_count: (.collisions | length)
}' "$SNAPSHOT"

jq -e --arg head "$ALEMBIC_HEAD" '
  .version == 1
  and .alembic_revision == $head
  and (.contexts | length) == 7
  and ([.contexts[].values.project_key] | sort) == [
    "red-games", "red-gift", "red-phone", "red-quant",
    "red-shrik", "red-viewer", "red-writer"
  ]
  and (.collisions | length) == 0
' "$SNAPSHOT" >/dev/null

EXPECTED_LOCAL_FILES="$(jq -er '.local_files | length' "$SNAPSHOT")"
EXPECTED_POLLUTED="$(jq -er '.polluted_plan_ids | length' "$SNAPSHOT")"
readonly EXPECTED_LOCAL_FILES EXPECTED_POLLUTED
```

Reconcile `local_file_count` with the current seven repositories. Historical ticket counts are
not acceptance values. Stop on an unexpected file count, project, owner, hash, path, collision,
database identity, schema revision, or context value. Have the operator approve the immutable
snapshot digest before mutation.

## 5. Apply canonical context paths

Reconfirm that writers remain off, the tested backup remains available, and both private digests
still match. Apply only the canonical `plan_scan_paths` and signed `updated_at` values:

```bash
test "$(sha256sum -- "$SNAPSHOT" | cut -d ' ' -f 1)" = "$SNAPSHOT_SHA256"
test "$(sha256sum -- "$BACKUP_RECEIPT" | cut -d ' ' -f 1)" = "$BACKUP_RECEIPT_SHA256"

uv run python scripts/repair_plan_index.py apply-paths \
  --run-id "$RUN_ID" \
  --snapshot "$SNAPSHOT" \
  --snapshot-sha256 "$SNAPSHOT_SHA256" \
  --backup-receipt "$BACKUP_RECEIPT" \
  --backup-receipt-sha256 "$BACKUP_RECEIPT_SHA256" \
  --postgres-restore-tested \
  --writers-off-confirmed
```

The first successful application returns `status="applied"` and `affected_rows=7`. An exact
idempotent replay may return `status="already_applied"` and `affected_rows=0`. Any other result or
CAS conflict blocks deployment and reindexing.

## 6. Keep the normal runtime unpublished

Do not run the installer while repair mutations are incomplete. Start only the authorized,
bounded maintenance MCP from the verified checkout for the seven calls below. Its unit override
must disable automatic restart, set a bounded runtime, and keep Dream, graph jobs, automation,
stdio clients, the watchdog service, and the watchdog timer stopped. Stop this MCP before
verification. Section 11 publishes the normal runtime after finalization.

## 7. Reindex one project at a time

Invoke `brain_reindex_plans` in seven separate MCP calls, in this fixed order:

```text
brain_reindex_plans(project_key="red-games")
brain_reindex_plans(project_key="red-gift")
brain_reindex_plans(project_key="red-phone")
brain_reindex_plans(project_key="red-quant")
brain_reindex_plans(project_key="red-shrik")
brain_reindex_plans(project_key="red-viewer")
brain_reindex_plans(project_key="red-writer")
```

After each call, capture its exact `indexed`, `skipped`, `linked`, `errors`, and `chunks_created`
counters. Stop immediately if `errors` is non-zero; do not run `finalize` after a partial or
failed reindex.

Create a private version-1 evidence file bound to the snapshot digest. It must contain exactly one
object for each project above, with captured non-negative integer counters and `errors: 0`.

```json
{
  "version": 1,
  "snapshot_sha256": "REPLACE_WITH_SNAPSHOT_SHA256",
  "projects": [
    {"project_key":"red-games","indexed":0,"skipped":0,"linked":0,"errors":0,"chunks_created":0},
    {"project_key":"red-gift","indexed":0,"skipped":0,"linked":0,"errors":0,"chunks_created":0},
    {"project_key":"red-phone","indexed":0,"skipped":0,"linked":0,"errors":0,"chunks_created":0},
    {"project_key":"red-quant","indexed":0,"skipped":0,"linked":0,"errors":0,"chunks_created":0},
    {"project_key":"red-shrik","indexed":0,"skipped":0,"linked":0,"errors":0,"chunks_created":0},
    {"project_key":"red-viewer","indexed":0,"skipped":0,"linked":0,"errors":0,"chunks_created":0},
    {"project_key":"red-writer","indexed":0,"skipped":0,"linked":0,"errors":0,"chunks_created":0}
  ]
}
```

The zero counters above are placeholders, not expected results. Replace them with the exact seven
tool results and replace the snapshot digest before saving. Then validate the closed schema:

```bash
chmod 0600 "$REINDEX_EVIDENCE"
jq -e --arg snapshot "$SNAPSHOT_SHA256" '
  .version == 1
  and .snapshot_sha256 == $snapshot
  and (.projects | length) == 7
  and ([.projects[].project_key] | sort) == [
    "red-games", "red-gift", "red-phone", "red-quant",
    "red-shrik", "red-viewer", "red-writer"
  ]
  and all(.projects[];
    .errors == 0
    and all([.indexed, .skipped, .linked, .errors, .chunks_created][];
      type == "number" and . >= 0 and floor == .
    )
  )
' "$REINDEX_EVIDENCE" >/dev/null
REINDEX_EVIDENCE_SHA256="$(sha256sum -- "$REINDEX_EVIDENCE" | cut -d ' ' -f 1)"
readonly REINDEX_EVIDENCE_SHA256
```

Stop MCP again after the seventh result, then reconfirm every writer is off before continuing:

```bash
systemctl --user stop brain-mcp-http.service
test "$(systemctl --user show -p ActiveState --value brain-mcp-http.service)" = inactive
```

## 8. Verify the canonical corpus

Verification is read-only for PostgreSQL and writes one private report. It recomputes local file
hashes and checks canonical ownership, paths, hashes, chunks, links, polluted-row stability, and
the target database identity.

```bash
test ! -e "$VERIFICATION_REPORT" && test ! -L "$VERIFICATION_REPORT"
uv run python scripts/repair_plan_index.py verify \
  --run-id "$RUN_ID" \
  --snapshot "$SNAPSHOT" \
  --snapshot-sha256 "$SNAPSHOT_SHA256" \
  --reindex-evidence "$REINDEX_EVIDENCE" \
  --reindex-evidence-sha256 "$REINDEX_EVIDENCE_SHA256" \
  --verification-output "$VERIFICATION_REPORT"
test "$(stat -c '%a' -- "$VERIFICATION_REPORT")" = 600
VERIFICATION_REPORT_SHA256="$(sha256sum -- "$VERIFICATION_REPORT" | cut -d ' ' -f 1)"
readonly VERIFICATION_REPORT_SHA256
```

Require `status="verified"`, `canonical_plan_count=$EXPECTED_LOCAL_FILES`, and
`polluted_plan_count=$EXPECTED_POLLUTED`. Confirm the report contains one canonical row per local
file:

```bash
jq -e --argjson expected "$EXPECTED_LOCAL_FILES" '
  .version == 1
  and (.canonical_plans | length) == $expected
  and (.evidence.projects | length) == 7
  and all(.evidence.projects[]; .errors == 0)
' "$VERIFICATION_REPORT" >/dev/null
```

Any mismatch blocks finalization.

## 9. Finalize exact polluted rows

Reconfirm writers-off state and every digest. Finalization deletes feature links for the exact
snapshotted polluted plan IDs, then those exact plan rows; plan chunks delete through the existing
foreign-key cascade. The transaction rejects any count or CAS drift.

```bash
FINALIZE_RESULT="$EVIDENCE_DIR/finalize-result.json"
test ! -e "$FINALIZE_RESULT" && test ! -L "$FINALIZE_RESULT"
umask 077
uv run python scripts/repair_plan_index.py finalize \
  --run-id "$RUN_ID" \
  --snapshot "$SNAPSHOT" \
  --snapshot-sha256 "$SNAPSHOT_SHA256" \
  --backup-receipt "$BACKUP_RECEIPT" \
  --backup-receipt-sha256 "$BACKUP_RECEIPT_SHA256" \
  --postgres-restore-tested \
  --writers-off-confirmed \
  --verification-report "$VERIFICATION_REPORT" \
  --verification-report-sha256 "$VERIFICATION_REPORT_SHA256" \
  > "$FINALIZE_RESULT"
chmod 0600 "$FINALIZE_RESULT"
jq -e --argjson expected "$EXPECTED_POLLUTED" '
  .status == "finalized" and .affected_rows == $expected
' "$FINALIZE_RESULT" >/dev/null
```

Do not retry blindly after an operational error. Preserve every artifact and investigate the
masked error type and database transaction state first.

## 10. Validate exact post-finalize counts

Run a new read-only inventory into a new file. Do not overwrite the original snapshot.

```bash
POST_RUN_ID="$(uv run python -c 'from uuid import uuid4; print(uuid4())')"
POST_SNAPSHOT="$EVIDENCE_DIR/post-finalize-snapshot.json"
test ! -e "$POST_SNAPSHOT" && test ! -L "$POST_SNAPSHOT"
uv run python scripts/repair_plan_index.py inventory \
  --run-id "$POST_RUN_ID" \
  --manifest "$MANIFEST" \
  --snapshot-output "$POST_SNAPSHOT"

jq -e --argjson expected "$EXPECTED_LOCAL_FILES" --arg head "$ALEMBIC_HEAD" '
  .version == 1
  and .alembic_revision == $head
  and (.contexts | length) == 7
  and all(.contexts[]; .values.plan_scan_paths == .proposed_plan_scan_paths)
  and (.local_files | length) == $expected
  and (.indexed_plans | length) == $expected
  and (.polluted_plan_ids | length) == 0
  and (.missing_canonical_files | length) == 0
  and (.collisions | length) == 0
  and all(.indexed_plans[]; .declared_chunk_count == .observed_chunk_count)
' "$POST_SNAPSHOT" >/dev/null
```

These are the exact acceptance counts: seven canonical contexts, one correctly owned indexed row
per current local file, no polluted row, no missing canonical file, no collision, and matching
declared/observed chunk counts. Retain the finalizer result as the exact deleted-plan count; the
finalizer has already required exact feature-link and plan deletion row counts in one transaction.
Do not reopen writers until an operator reviews and signs off all evidence.

## 11. Publish the normal runtime last

Keep all writers stopped. Validate the installer and render the MCP units into a private directory
outside systemd. Inspect that artefact before explicitly authorizing its bounded live publication:

```bash
set -euo pipefail
TMPDIR="${TMPDIR:-/tmp}"
render_parent="$(mktemp -d "$TMPDIR/systemd-render.XXXXXX")"
render_dir="$render_parent/units"
trap 'rm -rf -- "$render_parent"' EXIT
chmod 0700 "$render_parent"
test ! -e "$render_dir" && test ! -L "$render_dir"

./deploy/systemd/install.sh --check-only
./deploy/systemd/install.sh --render-dir "$render_dir"
test -d "$render_dir" && test ! -L "$render_dir"
for unit in \
  brain-mcp-http.service \
  brain-mcp-http-watchdog.service \
  brain-mcp-http-watchdog.timer; do
  test -f "$render_dir/$unit"
done
```

`--render-dir` only creates a private artifact; it never publishes live. After the inspection,
run the [canonical MCP publication preflight](../deploy/systemd/MCP_HTTP_RUNBOOK.md#preflight)
code block exactly as written, from `set -euo pipefail` through both `systemctl --user show`
commands. That explicit live operation repeats the non-publishing render, neutralizes the watchdog,
backs up the existing units, atomically publishes only `brain-mcp-http.service`,
`brain-mcp-http-watchdog.service`, and `brain-mcp-http-watchdog.timer`, then reloads and verifies
systemd. Do not run `daemon-reload` or restart the service before that block succeeds.

Only after the canonical publication preflight succeeds, start the normal MCP service:

```bash
systemctl --user restart brain-mcp-http.service
systemctl --user is-active brain-mcp-http.service
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8765/health >/dev/null
```

Make one read-only MCP call and confirm it targets the expected database. Run the watchdog one-shot
check, then enable its timer as the final activation. Do not start Dream, graph, or other writers
until the operator has accepted every repair and recovery artifact.

## Rollback before finalization

Before `finalize` succeeds, the bounded rollback restores the seven original `plan_scan_paths` and
`updated_at` values, then removes only canonical rows proven to have been created by this repair.
Stop writers and use the original snapshot and backup proof:

```bash
uv run python scripts/repair_plan_index.py rollback-before-finalize \
  --run-id "$RUN_ID" \
  --snapshot "$SNAPSHOT" \
  --snapshot-sha256 "$SNAPSHOT_SHA256" \
  --backup-receipt "$BACKUP_RECEIPT" \
  --backup-receipt-sha256 "$BACKUP_RECEIPT_SHA256" \
  --postgres-restore-tested \
  --writers-off-confirmed
```

Accept only `rolled_back` or an exact idempotent `already_rolled_back`. Re-inventory into a new
private file and compare the seven contexts and original plan set with the control snapshot before
reopening any writer.

## Recovery after finalization

After `finalize`, in-place repair rollback is forbidden. The control snapshot cannot reconstruct
deleted contents or embeddings. Restore the complete PostgreSQL database from the exact tested
custom-format backup named by the receipt.

Keep every writer, MCP process, pooler, and watchdog off. Gate every captured application login
role with `NOLOGIN`, terminate its target backends, and prove that fresh application connections
fail while the distinct maintenance login remains usable. Verify that `pg_restore --list` exposes
one nonempty `dbname` header equal to the signed production database name. The maintenance service
must target the same cluster and a different maintenance database; never accept an operator-typed
database name as authority.

Run the bounded restore through that maintenance service so the archive recreates its own database
and database-level attributes:

```bash
set -euo pipefail
PGSERVICE_PRODUCTION_MAINTENANCE=REPLACE_WITH_APPROVED_MAINTENANCE_SERVICE
BACKUP_ARCHIVE=/ABSOLUTE/PATH/TO/THE/TESTED/FULL-BACKUP.dump
readonly PGSERVICE_PRODUCTION_MAINTENANCE BACKUP_ARCHIVE
timeout -s KILL 30m pg_restore \
  --exit-on-error --clean --if-exists --create \
  --dbname="service=$PGSERVICE_PRODUCTION_MAINTENANCE" \
  "$BACKUP_ARCHIVE"
```

**Why `--if-exists`, and what it does not change.** Without it the command aborts on its own
first statement, `DROP DATABASE brain`, whenever the target is absent: `--clean` emits the drop
and `--exit-on-error` makes the missing database fatal. Measured rc=1 on 2026-09-02, again on
2026-09-03, and once more on a disposable bench that day. `--if-exists` changes the emitted SQL in
exactly ONE case — the object is not there — and that is the case that fails. Where the database
exists, which is the disaster this section rehearses, both forms emit the same drop and the same
restore. An earlier version of this paragraph argued the opposite, that adding the flag "would
change the command this runbook exists to rehearse"; the bench says otherwise, and a disaster
command that fails on an empty cluster is a hypothesis rather than a procedure.

**Priming, measured rather than assumed.** The archive recreates its own database, its encoding
and its extensions — none of those need a bench gesture. What it does NOT carry is its roles, and
`--exit-on-error` makes each missing one fatal at a different statement:

| Role | Attributes | Why the restore needs it |
|---|---|---|
| `brain` | `SUPERUSER LOGIN CREATEDB CREATEROLE REPLICATION` | owner of 369 archived objects; without it the restore stops at `ALTER DATABASE brain OWNER TO brain` |
| `codex_ro` | `LOGIN` | not an owner — a GRANTEE; without it the restore stops at `GRANT USAGE ON SCHEMA public TO codex_ro` |

`codex_ro` is the one a reader misses: it appears nowhere in the archive's owner column, so
listing the TOC owners finds only `brain` and the second failure arrives after the first is fixed.
Create both before replaying, on the bench and on any rebuilt cluster:

```sql
CREATE ROLE brain SUPERUSER LOGIN CREATEDB CREATEROLE REPLICATION;
CREATE ROLE codex_ro LOGIN;
```

Then VERIFY they exist rather than assuming the statement ran — `select rolname from pg_roles
where rolname in ('brain', 'codex_ro')` must return both.

On any restore or validation failure, leave application roles `NOLOGIN`, quarantine the target,
and keep every writer off. After a successful restore, use the gated maintenance-to-target service
to prove the recreated database fingerprint, catalog, and Alembic head. **Measure that head; do not
expect the one you left.** An archive taken from current production comes up at the head in
[Current targets](#current-targets) and needs no upgrade. An older archive comes up behind it and
must be brought to that head with `alembic upgrade head` before anything else, because the repair
CLI and the MCP are both fail-closed on it. Never stop at whatever head the archive happened to
carry: a receipt collected there attests a database the deployed code cannot serve, and it will
read as a pass.

Then run the recovery contract named in [Current targets](#current-targets) against the restored
target — the `-pgrestore` variant — and retain its full receipt and a provenance bundle. A short
receipt is a failed restore, not a stale gate: the extension check on the restored target is
names-only, so a healthy version drift is stated in the receipt, never failed (see the
names-only note in [Current targets](#current-targets)). Then replay the ACL contract's
`-pgrestore` twin, also named in [Current targets](#current-targets), against the same restored
target: this command preserves owners and grants (measured: 0 owner, 0 grant, 0 role-privilege
mismatches), and the twin attests exactly that, tolerating — and naming in its receipt — the
one maintenance superuser that performed the restore, `postgres`. The old derogation
(`--no-owner --no-acl` erases ownership) belongs to the sandbox rehearsal only, never to this
path. Only then restore
the captured login flags, reopen the database, start the MCP built for that head, pass health and
read-only canaries, and enable the watchdog timer last.

This is a full-database restore, not a selective table, row, plan, link, or chunk repair. Preserve
the failed repair evidence and the restore evidence; never replace the tested backup with a new,
post-failure backup.
