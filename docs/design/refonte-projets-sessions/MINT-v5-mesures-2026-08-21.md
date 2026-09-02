# Mint v5 — the measurements, frozen

> **Status: MEASUREMENTS TAKEN, ASSETS NOT WRITTEN.** This document exists so
> that writing the v5 assets is mechanical and verifiable, rather than redone
> from memory. All values below are **measured on 2026-08-21**, with the exact
> expressions from the v4 contract — not recomputed by hand.
>
> Doctrine: **S1** (decision `9d22bc6a`) — `alembic_head` **DERIVED**, a single
> mint for the whole lane. Scope: **v5 MINIMAL** (decision `567f6298`) —
> 25 checks, 046's mechanisms; the 041 trigger check moves to a follow-up ticket
> (`23962510`).

## 0. The mint's source, and why it isn't production

Prod is at **`045`**. The v5 assets must describe the **post-046** state. The mint
is therefore struck against **`brain_test`**, which is at `046`.

**And this source has been VALIDATED, not assumed.** A diverging mirror would let
its divergence leak into the attestation:

```
prod: 129 indexes    brain_test: 130 indexes
diff → a single line: + public.brain_sessions.uq_brain_sessions_connection
tables → IDENTICAL (32 on both sides)
```

One gap, exactly the one from 046. This is the only evidence that authorizes
minting somewhere other than prod.

## 1. Reference receipt BEFORE the mint

`22/25` measured on 2026-08-20 against prod at `045` (thread `eb067b57`). Three
failures, **all benign**, all traced to a migration postdating the v4 mint (frozen at `039`):

| Failure | Cause |
|---|---|
| `alembic_head` 039 ≠ 045 | six revisions behind — **this is what S1 removes** |
| `catalog_counts.indexes` 128 ≠ 129 | `idx_dream_runs_date_project`, migration **042** |
| `view_column_mismatches` = 1 | `codex_dream_run_v1`, `model` column widened by **045** |

046 adds two more: the connection index (129 → **130**) and the column
fingerprint of `brain_sessions` (+5 columns).

**Do not copy this 22/25 forward: replay it.** It will change with every head.

## 2. The measured values — `brain_sessions`

Expressions taken **literally** from the v4 contract:
- indexes: `md5(pg_get_indexdef(indexrelid))`
- constraints: `md5(regexp_replace(lower(pg_get_constraintdef(oid, TRUE)), '[[:space:]]+', ' ', 'g'))`

### `expected_session_indexes` — CLOSED list, 3 → 4 entries

| index | md5 | state |
|---|---|---|
| `brain_sessions_pkey` | `6763cd8159ef6f0131abbfedfea044bc` | unchanged |
| `idx_brain_sessions_project_status_started` | `daf2b70c6799177168837efedcb0dbe8` | unchanged |
| `uq_brain_sessions_project_client` | `28c33a3d73bf9f0c64d322978b7118a4` | unchanged |
| **`uq_brain_sessions_connection`** | **`62b298d247237eddf60cb4ba28693af4`** | **NEW** |

⚠️ This list is checked **TWICE** (`v4.sql:665` absent-or-md5-diverging,
`:687` present-outside-the-list). One forgotten entry breaks it both ways.

### `expected_session_constraints` — 8 → 9 entries

| constraint | v4 | v5 |
|---|---|---|
| `brain_sessions_status_valid` | `4f21eff965e8da6178bb2d1030fc03f8` | **`f5065acef0a32bfc97e66f6d802b9585`** |
| `brain_sessions_terminal_state_valid` | `9abfd0c69ce694043e32e1935d17ff4f` | **`aab51404804e113ec2c452ba0bc21aa8`** |
| **`brain_sessions_nature_valid`** | — | **`b3899128eb71e5e3023e994b0f1e26db`** (`'c'`, `NULL::text`) |

Unchanged: `capture_ids_valid` `1a8756bd34b4ea7e8d835643d0fa7ceb`,
`client_key_nonblank` `8ec1e8c3738bbe2178e04689dd038e0d`,
`focus_outcome_valid` `8d97a41480c2c3b6ec5a87bf0e64fb03`,
`pkey` `cc3552dbb61b18accca876af5296eb1f`,
`project_key_fkey` `b863ba166c02670d9dad0a56f9582d59` (`'f'`, `'r'`),
`uq_brain_sessions_project_client` `153c25b1acb665316ea262444b4d0d79`.

### `expected_session_constraint_fragments` — a FOURTH status literal

The observed definition, normalized, is now:

```
check (status::text = any (array['open'::character varying, 'ended'::character varying,
'abandoned'::character varying, 'closed_inactive'::character varying]::text[]))
```

The code block hardcodes the status literals: it now needs **four**.

### `catalog_counts`

`indexes`: **128 → 130**. `foreign_keys`: **26**, unchanged. `table_set`:
**unchanged**, no new table (32 with `alembic_version`).

## 3. What remains to be WRITTEN, and the trap not to trigger

1. **`alembic_head` becomes DERIVED** (S1) — this is the only DESIGN part. The
   v4 check is `{"kind": "alembic_head_equals", "revision": "039"}` and its CTE
   lives at `v4.sql:1766-1791`. The invariant becomes "a single head, consistent
   with the repository", template `test_alembic_env.py:254-259` ("The head is
   DERIVED, not pinned"). The exact revision remains proven by `_REQUIRED_ALEMBIC_HEAD`.

2. ⚠️ **NEVER A GLOBAL `sed` ON "039".** `v4.sql` carries **seven**
   occurrences, and **five name the invariant installed BY 039** —
   `recovery_039_observation` (lines 1766, 1964, 1966) and
   `project_context_updated_at_039` (line 1962). **Only two** are the head pin
   (lines 1789, 1791). A global replacement would corrupt the asset **silently**.

3. **The third asset: `brain-v42-v4.json`.** Named **zero times** in the five
   design documents. Its regeneration **is not an edit**:
   `test_v4_json_is_the_exact_v3_delta` derives it from `v3.json` and asserts the
   byte — `_expected_v5()` must be written on the same template, from `v4.json`.

4. **CTE parity** between `v5.sql` and `v5-pgrestore.sql`: allowed gap =
   exactly `{observed_artifact_constraints, observed_session_constraints}`
   (`test_recovery_contract_v4_pgrestore.py:29-33`).

5. **The runbook's two gates** (`PLAN_INDEX_REPAIR_RUNBOOK.md`): it announces
   `24/25` and requires `25/25` as an authorization gate before `repair` — two
   places, and the document already contradicts itself.

6. **`chmod 0600`** on the v5 assets as soon as they're created (runbook line 45).
   Done for `brain-v42-v4.sql` on 2026-08-20; the 11 assets are uniform today.

## 4. What this mint does NOT do

The `content_updated_at_041` trigger check (~270 lines of catalog SQL) stays
**out of scope** — follow-up ticket `23962510`. The v5 contract therefore keeps
**25 checks**, not 26.

---

*Measurements taken on 2026-08-21 between 06:05 and 06:20, read-only, against
`brain_test` at head `046`. No write, no migration run against `brain`.*
