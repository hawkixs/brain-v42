---
title: "Alembic fail-closed — explicit migration target and production opt-in"
status: completed
summary: "Remove every implicit fallback to the live database: POSTGRES_URL becomes mandatory for Alembic, the DSN disappears from alembic.ini, and the brain database requires an explicit production opt-in."
tags:
  - alembic
  - prod-safety
  - fail-closed
  - pattern-auto
  - sol-ultra
---

# Alembic fail-closed — explicit migration target and production opt-in

> Source: SA1 workstream from
> `docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md`.
> Branch: `codex/startup-fail-closed-schema-gate`.
> Pattern: pattern-auto, plan to be validated before any code change.

## Goal

Prevent Alembic from implicitly selecting the live `brain` database. A migration must
receive `POSTGRES_URL` in the process environment. If the variable is missing, if its URL
is invalid, or if it targets `brain` without explicit confirmation, Alembic must stop
before any connection attempt.

The June 30 fix stays in place: the `POSTGRES_URL` override and the integration-test guard
work. This workstream closes only the third level still open, the settings/`.env` fallback
then `alembic.ini`.

## Architecture

`alembic/env.py` becomes the single validation boundary for the migration target:

1. read only `POSTGRES_URL` from the process environment;
2. parse the URL with `sqlalchemy.engine.make_url`;
3. reject any query string, because the asyncpg dialect can use it to overwrite the host,
   port or database after path validation;
4. require the `postgresql+asyncpg` driver, a host, a TCP port in `1..65535`, a username,
   a password and a non-empty database name;
5. if the name is exactly `brain`, require
   `BRAIN_ALEMBIC_ALLOW_PROD=1|true|yes`;
6. return the DSN without ever logging it;
7. inject that DSN into the Alembic config before online or offline mode.

`alembic.ini` keeps the `sqlalchemy.url` key, required by the Alembic contract, but its
value stays empty. It no longer contains any host, user, password or database name.

## Non-goals

- Do not create or modify a live PostgreSQL role: that requires a separate DB operation and
  a deployment decision.
- Do not add a schema-version gate at MCP server startup; that is a separate workstream if a
  runtime need is demonstrated.
- Do not run any migration against the live database during this workstream.
- Do not modify `AGENTS.md`, `CLAUDE.md` or `uv.lock`, already modified before this session.
- Do not change migrations 001–031.

## Blast radius

GitNexus, before the plan: risk **LOW**, one direct caller (`alembic/env.py`), no process and
no application module affected. The operational blast radius stays high by nature: every
Alembic call without an explicit env will stop working, on purpose.

## File structure

| File | Change |
|---|---|
| `alembic/env.py` | Fail-closed resolution, parsing and prod opt-in |
| `alembic.ini` | Removal of the live DSN, empty `sqlalchemy.url` value |
| `tests/unit/test_alembic_url_resolution.py` | TDD tests for the resolver and redaction |
| `tests/unit/test_alembic_cli_fail_closed.py` | Hermetic subprocess contracts, real Alembic wiring |
| `tests/unit/test_alembic_env.py` | Structural contract: no DSN in the ini |
| `README.md` | Operator command with explicit prod opt-in |

## Worktree preservation gate

Before the RED phase, record the SHA-256 and the diff hash of `AGENTS.md`, `CLAUDE.md`
and `uv.lock`. Redo the same calculations before every commit. All Python commands
use `uv run --frozen` so the lockfile is never synced or rewritten.

After every implementation task, pattern-auto requires two checkpoints before continuing:
a plan-compliance review, then a quality/test review of the task's diff.

## Task 1 — Lock the resolver contract in RED

**Files:**

- Modify: `tests/unit/test_alembic_url_resolution.py`

1. Adapt `_get_resolver()` to the argument-free contract.
2. Keep the `POSTGRES_URL` priority test, renamed as an explicit acceptance test.
3. Replace the fallback tests with the following cases:
   - missing env → `RuntimeError` mentioning `POSTGRES_URL`, without importing settings;
   - malformed URL containing a sentinel secret → generic error raised `from None`,
     sentinel absent from the cause and the traceback;
   - query string present, notably `?database=brain` on `/brain_test` → rejected without leak;
   - driver other than `postgresql+asyncpg` → rejected;
   - missing database → rejected;
   - missing host, port, username or password, and out-of-range port → rejected before connection;
   - `brain_test` database → accepted without opt-in;
   - `brain` database → rejected without opt-in;
   - `brain` database → accepted with `BRAIN_ALEMBIC_ALLOW_PROD=1`, `true` and `yes`;
   - unknown opt-in value → rejected.
4. Run the targeted module and observe the failures before implementation:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest tests/unit/test_alembic_url_resolution.py -q
```

Expected RED: the current resolver still accepts an argument, reads settings and returns the
ini fallback.

## Task 2 — Implement the fail-closed boundary

**Files:**

- Modify: `alembic/env.py`
- Test: `tests/unit/test_alembic_url_resolution.py`

1. Import `make_url` from `sqlalchemy.engine`.
2. Replace `_resolve_sqlalchemy_url(default: str)` with a resolver that has no fallback.
3. Never interpolate the faulty DSN into an exception or a log.
   Every error conversion uses `raise RuntimeError(...) from None`.
4. Reject any query string before checking `drivername`, `database`, host, port
   `1..65535`, username, password and the `brain` database opt-in. The test documents that
   the asyncpg dialect lets `?database=brain` overwrite the `/brain_test` path in its
   effective connection arguments. Additional tests prove that no asyncpg default can
   implicitly supply the endpoint or the identity.
5. Keep the constants/allowlists used by the resolver inside the function so the
   AST test does not mask a global dependency; the CLI tests validate the full wiring.
6. Remove `_ini_url`; call the resolver, then inject
   `resolved_url.replace("%", "%%")` into `config.set_main_option`. The resolver keeps
   returning the original DSN.
7. Clean up the annotations/imports of `alembic/env.py` so the file passes mypy without
   `unused-ignore` or a `target_metadata` redefinition.
8. Run the targeted tests to GREEN.
9. Also run the existing DB guard tests:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/test_alembic_url_resolution.py \
  tests/unit/test_integration_db_guard.py -q
```

## Task 3 — Remove the static secret and document the prod operation

**Files:**

- Modify: `alembic.ini`
- Modify: `tests/unit/test_alembic_env.py`
- Modify: `README.md`

1. First write a test that rejects any non-empty value for `sqlalchemy.url` in
   `alembic.ini` and checks the absence of `brain:brain` in the ini and the README.
2. Observe the RED on the current ini.
3. Replace the DSN with `sqlalchemy.url =` and explain that `alembic/env.py` injects the
   explicit value.
4. Update the Quick Start:

```bash
export POSTGRES_URL="postgresql+asyncpg://brain:REPLACE_WITH_PASSWORD@localhost:5433/brain"
BRAIN_ALEMBIC_ALLOW_PROD=1 alembic upgrade head
```

The documentation must not republish the real password, including in the Configuration
section. It must specify that the opt-in is only required when the database name is exactly
`brain` and must stay one-off, never exported durably in the shell.
5. Rerun the two Alembic unit modules.

## Task 4 — Prove the CLI wiring and the 31 migrations

**Files:**

- Create: `tests/unit/test_alembic_cli_fail_closed.py`

1. Write subprocess tests with `sys.executable -m alembic`, a cleaned environment,
   a fixed `cwd`, timeout and captured stdout/stderr. Use a temporary config whose
   `script_location` points to the real Alembic directory and a sentinel `.env` in the
   temporary cwd. Prove that, without a process `POSTGRES_URL`, Alembic fails on the guard and
   does not use that `.env`.
2. Test a malformed URL whose port contains a sentinel secret. Check the full subprocess
   stderr and the absence of the sentinel.
3. Test an encoded password containing `%40` in offline mode. The call must succeed and the
   sensitive value must be absent from stderr.
4. Automate the following two offline contracts:

```bash
POSTGRES_URL="postgresql+asyncpg://brain:encoded%40secret@localhost:5433/brain_test" \
  env -u VIRTUAL_ENV uv run --frozen alembic upgrade head --sql
```

Expected: `brain_test` renders the 31 migrations without a connection; `brain` without opt-in
fails before SQL rendering.

5. Do not run the prod case with opt-in: its acceptance is covered by the unit resolver
   and the documentation; a real live migration is out of scope.
6. Start an ephemeral PostgreSQL/pgvector instance named `brain_test`, on a dedicated loopback
   port and without a host volume. Actually apply `alembic upgrade head`, check `alembic current`
   and `alembic heads`, then destroy only that temporary container. Do not reuse the
   `brain_v42_postgres` container, port 5433, or a DSN taken from `.env`.
7. Record a separate Brain decision about the migration role: recommend a dedicated
   `brain_migrator` role, the required DDL permissions and the bootstrap cost; application
   deferred to an explicitly authorized live operation.

## Task 5 — Gates and branch review

1. Run the targeted gates:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/test_alembic_url_resolution.py \
  tests/unit/test_alembic_cli_fail_closed.py \
  tests/unit/test_alembic_env.py \
  tests/unit/test_integration_db_guard.py -q
env -u VIRTUAL_ENV uv run --frozen ruff check alembic/env.py tests/unit/test_alembic_url_resolution.py tests/unit/test_alembic_cli_fail_closed.py tests/unit/test_alembic_env.py
env -u VIRTUAL_ENV uv run --frozen ruff format --check alembic/env.py tests/unit/test_alembic_url_resolution.py tests/unit/test_alembic_cli_fail_closed.py tests/unit/test_alembic_env.py
env -u VIRTUAL_ENV uv run --frozen mypy alembic/env.py src/
```

2. Run the full unit suite.
3. Apply `gitnexus_detect_changes(scope="all")`.
4. Have the full diff reviewed by a final judge. Expected verdict: `SHIP`.
5. Index this plan in Brain, pin its feature to `building`, then move it to
   `deployed` or `done` only after proof and merge.

## Acceptance criteria

- Alembic without `POSTGRES_URL` fails before connecting, even if `.env` exists.
- An invalid `POSTGRES_URL` never appears in the error or its cause chain.
- Any query string is rejected before connecting; it cannot overwrite the validated target.
- Host, TCP port, username and password are explicit; no asyncpg default completes
  the target or the identity.
- A valid DSN containing `%` crosses ConfigParser and offline mode without a leak.
- No real DSN or credential is stored in `alembic.ini` or added to the README.
- `brain_test` works without opt-in; `brain` requires an explicit opt-in.
- The 31 migrations actually apply on an ephemeral PostgreSQL/pgvector instance and reach
  the expected head.
- The existing migrations and their order stay unchanged.
- The unit tests and static gates are green.
- The worktree's pre-existing modifications stay unstaged and unchanged.

## Execution evidence — July 11, 2026

The pattern-auto converged after two judging passes on the plan, then each slice received
an independent spec review and quality review. The resolver's first review found
a leak still introspectable via `RuntimeError.__context__`; the fix moved the
generic raise out of the `except` block, then both reviewers returned `SHIP`.

Functional and static evidence:

- resolver and DB guard: 34 tests passed;
- targeted Alembic contracts: 66 tests passed;
- full unit suite: 2,901 tests passed, 39 skipped, 8 pre-existing warnings;
- Ruff check and format: passed;
- mypy: no issues across 112 source files;
- `AGENTS.md`, `CLAUDE.md` and `uv.lock` keep their initial hashes and stay out of
  staging.

The four CLI tests prove the final wiring. Their first failure (62 occurrences instead of
31) was a test counting bug between stdout and stderr, not a new defect produced;
the RED proof produced comes from the historical resolver and the static DSN in `alembic.ini`.

The final review then found a second P1: with `/brain_test?database=brain`, SQLAlchemy
still exposed `brain_test` in the parsed URL but the asyncpg dialect actually transmitted
`database=brain`. Four RED tests (`database`, `host`, `port`, `ssl`) proved the bypass.
Alembic now rejects any query string; 60/60 targeted contracts are green after this patch.
The same review then showed that a DSN without host, port or identity let asyncpg pick
implicit defaults. Six RED tests closed this last path:
endpoint and identity are now complete, and 66/66 targeted contracts are green.

This session's execution report — not a raw transcript kept — indicates that the
real drill used the pinned pgvector runtime image, a single tmpfs container, no
bind/volume and a random loopback port. Two first attempts stopped before
migration on harness errors, with exact-CID cleanup verified. The third applied
`001 -> 031` on `brain_test` with `BRAIN_ALEMBIC_ALLOW_PROD` absent. Results:

- `alembic current` and `alembic heads`: `031 (head)`;
- `alembic_version`: `031`; pgvector: `0.8.4`;
- 23 public tables, `codex_brain_entity_v1` view present, `codex_ro` role present;
- no invalid or not-ready index;
- disposable container destroyed and `brain_v42_postgres` ID identical before/after.

Proof limit: the transcript and the final CID of the disposable container were not
kept. A read-only post-session check confirms the main container `running` on
the pinned image and the absence of any container carrying the drill's label. A runbook with
durable artifact capture belongs to future DR/OPS hardening.

The Brain decision `a665e495-3a92-4a46-852d-5c90177c6e06` records a future
`brain_migrator` role without cluster privileges, separates privileged bootstrap from
routine migrations and defers any live mutation.
