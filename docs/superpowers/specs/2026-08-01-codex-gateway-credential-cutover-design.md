# Coordinated rotation of Codex gateway credentials

Date: 2026-08-01
Brain ticket: `52d6b319-3527-45bd-a211-058e17bfbfa9`

## Goal and starting state

The gateway cutover is blocked by three secrets not yet provisioned in a
coordinated way: the PostgreSQL password of the `brain` owner role, that of the read
role `codex_ro`, and the HTTP bearer shared between the gateway and `red-codex`. The repo already
has a solid pattern for Neo4j (`scripts/rotate_neo4j_credential.py`), but the gateway runbook only
provides a suite of manual operations.

The mechanism must cover the actually observed consumers:

- `brain_v42/.env` for the MCP HTTP, the automations and the Compose gateway;
- Dream access, indirectly via the MCP HTTP, and its timer during quiescence;
- `red-data/.env` for its two Dagster services;
- `/etc/shrik/env` for the `red-shrik` daemon;
- `red-codex/.env.local` for `codex_ro`, the private `:9211` URL and the bearer;
- `~/.config/brain-v42/codex-gateway.env` for the bearer on the gateway side.

Modifying these private files and recreating their services is a deployment operation. No
source file of `red-codex`, `red-data` or `red-shrik` belongs to the `brain_v42` diff. The
default ticket display therefore remains a separate coordination on the `red-codex` side.

## Options

### Option 1 — fail-closed coordinator with a fixed inventory (chosen)

Add a Python CLI in `brain_v42`, dry and read-only by default. The operator supplies
the canonicalized roots of `brain_v42` and ReD; the program derives from them a closed list of
files, services and keys. It refuses an unexpected path, a symlink, a duplicate key,
an unfixable overly-permissive file, an absent consumer or a failing preflight
command.

`--apply` mode:

1. takes an exclusive lock and writes a resumable `0600` journal in the private directory;
2. generates three distinct secrets without placing them in arguments, the environment, Git or
   the outputs;
3. prepares the complete files then quiesces Dream/MCP/automations, Dagster, Shrik,
   `red-codex` and a possible gateway;
4. changes `brain` and `codex_ro` in a single local PostgreSQL transaction;
5. atomically installs the private files and recreates only the affected consumers;
6. requires, on new connections, both new passwords accepted, both
   old ones explicitly refused, `codex_ro`'s bounded privileges, `/ready` green, the new
   bearer accepted and the old one refused;
7. deletes the journal only after all proofs.

An error triggers the rollback: stopping the restarted consumers, reversing the PostgreSQL
transaction, atomically restoring the private contents and returning to their initial activity state.
If the rollback cannot be proven, the journal remains present and the CLI fails with a
generic message; it never attempts to continue the cutover.

Advantages: repeatable, testable with simulated boundaries, secrets absent from logs, bounded
cutoff window, secret-free JSON result. Drawback: the host must authorize in advance
the Docker operations and the `sudo -n` strictly necessary for `/etc/shrik/env` and the
Shrik service.

### Option 2 — enriched manual runbook

Document the rotation order, keep the secrets in masked inputs and ask
the operator to modify then restart each consumer. This option requires less code,
but it cannot prove the completeness of the modified files, resume an interruption,
or automatically test the rollback. An error between `ALTER ROLE` and the last file can
leave several projects with different generations.

It is rejected for this cutover: the code savings do not offset the absence of an operational
transaction and reproducible proof.

## Bounded interface

The CLI accepts only non-secret inputs: absolute roots, private directory,
`--apply`, `--resume` or `--rollback`, and explicit operator confirmations. No password or
bearer argument exists. The external commands are hardcoded in the program; no
shell or arbitrary command comes from a manifest.

The mandatory dry-run validates at minimum:

- owners, types and modes of the six private files;
- expected keys, roles, hosts, ports and database in the DSNs, without rendering their values;
- `docker compose config --quiet` in the three projects;
- availability of `systemctl --user`, of Docker, of PostgreSQL and of `sudo -n` for Shrik;
- Alembic state exactly `037`, ten views, seven barriers and two triggers;
- gateway port exactly `9211`, with no host publish.

The result contains only booleans, counters, consumer identifiers and sanitized states. The
caught exceptions are never chained into the operator output.

## TDD and atomic batches

1. Dry contract tests: dry-run by default, closed inventory, modes, duplicates, DSN, no
   secret input in the parser or the outputs.
2. Cutover state-machine tests: journal/lock, quiesce-rotation-install-restart order,
   new connections, old refusals and `codex_ro` privileges.
3. Tests for each injected failure: database/files/states rollback, journal retained if rollback
   is incomplete, absence of secrets in errors and external calls.
4. CLI documentation and replacement of the runbook's manual recipes.

The live cutover remains forbidden before a fresh reviewer `APPROVE`, a fresh tester `PASS` on the
same HEAD and a real dry-run with a green preflight rollback. The mechanism never migrates Alembic:
the observed production stays at `037` for this ticket, even though the repo is at `039`. Revisions
038 and 039 remain outside this cutover and are never applied implicitly.

## Possible external link

The only potentially human link is the absence of a non-interactive authorization already
provisioned to write `/etc/shrik/env` and drive `red-shrik.service`. The coordinator must
detect it at dry-run before any mutation. No sudo password will be requested, stored or
invented; if `sudo -n` is unavailable, everything else can be shipped and this exact prerequisite remains
the sole blocker of the live cutover.

## Corrected privileged contract

The preflight calls exactly
`sudo -n /usr/local/sbin/brain-shrik-env-control --check`. The apply and rollback write their
payload to the fixed private staging
`~/.config/brain-v42/codex-gateway-rotation/.shrik-env.install`, then call exactly the same
`--publish`. The root-owned helper refuses any free target, unit or action; it publishes only
`/etc/shrik/env` as `root:hawixs 0640` via atomic replacement, fsync and full re-read.

The helper also bounds `--stop`, `--start` and `--is-active` to `red-shrik.service`. The versioned
sudoers enumerates these five exact argv. The old `tee` and `systemctl red-*` grants of red-shrik
do not constitute proof for this cutover; no `true` or `install` grant is added.
The single initial root gesture is `sudo ./deploy/install-brain-shrik-env-control.sh` from the
reviewed checkout. This installer modifies neither `/etc/shrik/env` nor the service state.
