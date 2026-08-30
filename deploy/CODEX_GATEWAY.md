# Codex gateway — private deployment via Docker Compose

This runbook deploys the administration gateway consumed by `red-codex`. It describes an
operator procedure; its presence in the repository does not prove a live deployment succeeded.

> **Live activation forbidden** as long as `codex_ro` and the owner account `brain`
> use their default development credentials. Rotation must include a fresh
> successful connection with the new secret and the proven refusal of the old one.

The gateway belongs exclusively to the Compose service `brain-codex-gateway`. It listens
on the logical port `9211` in `brain_v42_default`, with no port published on the host. This value
implements Brain decision `61574e9a-5ad3-457f-b0bb-58ec01f5e73a`; port `9210` remains reserved
for `red-shrik`. The consumer therefore uses the internal URL
`http://brain-codex-gateway:9211`.

The service carries the dormant Compose profile `codex-gateway`: a global `docker compose up -d`
never starts it. Only an explicit targeting of `brain-codex-gateway`, after this runbook's
gates have been validated, activates this profile.

Do not install, activate, or start any systemd unit for this gateway.
`deploy/systemd/install.sh` does not manage it.

## Preconditions

Run the commands from the root of `brain_v42`. Before deployment:

- PostgreSQL must be healthy in the Compose stack;
- Alembic migration `036` must be in the applied chain; current production must
  announce the Alembic head actually deployed, measured immediately before the procedure — never
  copy it from a previous run;
- the `codex_ro` and `brain` PostgreSQL credentials must be non-default and verified;
- the Dream killswitch file must exist on the host;
- the `red-codex` API must join the external network `brain_v42_default`;
- Docker Compose must be able to build the `production` target.

Migration `036` adds nine views and refreshes `codex_brain_entity_v1`, for a total of ten views
checked by readiness. Its `CREATE OR REPLACE` refreshes the existing view without changing
its OID or breaking its dependents. It adds `indexed_plans` to the discovery of `red`
sub-partitions, which makes visible the plans of a sub-partition present only in that
table. The tickets, features, and proposals families remain limited to the `red` group; the
Dream and consolidation views remain global by contract.

The seven views that filter the `red` group use `security_barrier=true`. This barrier
prevents PostgreSQL from pushing a function supplied by `codex_ro` under the confidentiality
filter and observing an out-of-scope row as a side effect.

The same migration adds two SQL fences:

- `trg_feature_artifact_live_target` refuses a new artifact toward an archived or
  merged feature;
- `trg_ticket_participants_immutable` forbids any modification of `from_project` or
  `to_project` after ticket creation.

```bash
# POSTGRES_URL is injected by the secrets manager, without displaying it.
BRAIN_ALEMBIC_ALLOW_PROD=1 uv run alembic current
```

The repository head advances with every merged migration; never copy its number from a previous
run. On this production, `alembic current` must announce the deployed head, measured before the
procedure. Migration 037 descends from 036 and keeps the ten views the gateway requires.
Migration 038 adds the Dream EXTRACT attempts log and migration 039 isolates the timestamp
trigger of `project_contexts`; no migration after 036 is part of the gateway cutover.
This runbook applies no Alembic migration beyond what production already carries.

Stop if the measured revision predates `036`; never downgrade to 036 to
satisfy this runbook. On a fresh environment, first run the main migration runbook
with its own authorization and proofs, then come back here.

## Clearing credential blockers

`scripts/rotate_codex_gateway_credentials.py` is the rotation authority for the
PostgreSQL roles `brain` and `codex_ro` as well as for the gateway bearer. It reuses the contract of the
Neo4j rotator: dry-run by default, closed inventory, private `0600` log, exclusive lock,
atomic writes, resume and rollback. It receives no secret via argument or environment
variable, and its JSON results contain only sanitized proofs.

The coordinator updates five private consumers: `brain_v42`'s `.env`, `red-data`'s
`.env`, `red-codex`'s `.env.local`, `/etc/shrik/env`, and
`~/.config/brain-v42/codex-gateway.env`. It also hardens the `red-data` file to `0600`.
All these processes pin their DSN or bearer at startup: rotation therefore unfolds in two
phases separated by their explicit recreation.

Install the privileged boundary once from the reviewed checkout. It is the only step that
requires an interactive sudo:

```bash
sudo ./deploy/install-brain-shrik-env-control.sh
```

The installer places `/usr/local/sbin/brain-shrik-env-control` as `root:root 0755` and its sudoers
drop-in as `root:root 0440`, validates the grants with `visudo`, then runs the read-only `--check`.
It neither rewrites nor displays the content of `/etc/shrik/env` and does not change the service state. The
only NOPASSWD commands are `--check`, `--publish`, `--stop`, `--start`, and `--is-active`.
Do not add any generic grant for `true`, `tee`, `install`, or `systemctl` to this workflow.

Prepare only the private parent, then launch the dry-run from the canonical root of
`brain_v42`. The command validates the files, the consistency of the old credentials, a
fresh TCP connection for each role, the scope of `codex_ro`, production exactly at the head
YOU declared — measured immediately before the procedure, never copied — and the
bounded non-interactive privilege `red-shrik` needs. It generates and writes no
secret:

```bash
set -euo pipefail
export BRAIN_ROOT="$(pwd -P)"
export RED_ROOT="/home/hawixs/hawkixs_infra/git_repo/ReD_v1"
export ROTATION_DIR="$HOME/.config/brain-v42/codex-gateway-rotation"
export SHRIK_ENV="/etc/shrik/env"
install -d -m 0700 "$(dirname "$ROTATION_DIR")"

# Measured, never copied: the guard used to compare against a constant `037`, and the
# procedure became inexecutable as soon as the next migration landed.
export DEPLOYED_HEAD="$(docker exec brain_v42_postgres \
  psql -U brain -d brain -Atc "select version_num from alembic_version;")"

uv run python scripts/rotate_codex_gateway_credentials.py \
  --brain-root "$BRAIN_ROOT" \
  --red-root "$RED_ROOT" \
  --private-dir "$ROTATION_DIR" \
  --shrik-env "$SHRIK_ENV" \
  --expected-alembic-revision "$DEPLOYED_HEAD"
```

The dry-run must finish with `status=preflight_ok`, `alembic_revision` equal to the head you
declared, `old_credentials_valid=true`, and `codex_scope_bounded=true`. Stop at the first
discrepancy. The CLI never applies Alembic and no longer accepts ANY revision implicitly:
`--expected-alembic-revision` is required and rejects an empty or malformed value.

The preflight's contract proof covers the FOUR clauses of `/ready`, not just
the views' existence: the ten views and their columns, `security_barrier=true` on the seven
scoped views, and the two active triggers (`trg_feature_artifact_live_target`,
`trg_ticket_participants_immutable`). A `CREATE VIEW` recreated without
`WITH (security_barrier=true)` — the exact move a column rotation forces — therefore
fails AT PREFLIGHT (`security_barrier:<view>` among the missing items), not only after cutover.

Capture the active state of units and containers without displaying their environment. Then
neutralize Dream, its auxiliary jobs, and all direct consumers before the window: MCP,
metrics, automation, the two `red-data` Dagster services, `red-shrik`, the `red-codex` API, and
any gateway. Verify that none of these processes is still active before attesting
quiescence to the CLI.

```bash
systemctl --user stop \
  brain-v42-dream.timer brain-v42-graph-recon.timer \
  brain-v42-embedding-backfill.timer brain-mcp-http-watchdog.timer
systemctl --user stop \
  brain-v42-dream.service brain-v42-graph-recon.service \
  brain-v42-embedding-backfill.service brain-v42-automation.service \
  brain-metrics.service brain-mcp-http-watchdog.service brain-mcp-http.service
docker compose stop brain-codex-gateway
docker compose -f "$RED_ROOT/projects/red-data/docker-compose.yml" \
  --env-file "$RED_ROOT/projects/red-data/.env" \
  stop dagster-webserver dagster-daemon
sudo -n /usr/local/sbin/brain-shrik-env-control --stop
if sudo -n /usr/local/sbin/brain-shrik-env-control --is-active; then
  echo "ERROR: red-shrik.service est encore actif" >&2
  exit 1
else
  status=$?
  [[ "$status" -eq 3 ]] || exit "$status"
fi
docker compose -f "$RED_ROOT/projects/red-codex/docker-compose.local.yml" \
  --env-file "$RED_ROOT/projects/red-codex/.env.local" stop api
```

Only use `--rollback-preflight-confirmed` after verifying that these same commands
can restore the captured state. The first phase creates a new generation, rotates both
roles in a single PostgreSQL transaction, installs the five files, and proves the new
passwords accepted and the old ones refused. It must stop at
`status=awaiting_consumer_recreation`; the log is then deliberately kept in place:

```bash
uv run python scripts/rotate_codex_gateway_credentials.py \
  --brain-root "$BRAIN_ROOT" \
  --red-root "$RED_ROOT" \
  --private-dir "$ROTATION_DIR" \
  --shrik-env "$SHRIK_ENV" \
  --expected-alembic-revision "$DEPLOYED_HEAD" \
  --apply \
  --consumers-stopped-confirmed \
  --rollback-preflight-confirmed
```

Recreate the read consumers and MCP first, then the private gateway on `:9211`, and
finally `red-codex`. Re-enable the Dream timers only after all the probes:

```bash
systemctl --user start \
  brain-mcp-http.service brain-mcp-http-watchdog.timer \
  brain-metrics.service brain-v42-automation.service
docker compose -f "$RED_ROOT/projects/red-data/docker-compose.yml" \
  --env-file "$RED_ROOT/projects/red-data/.env" \
  up -d --no-deps --force-recreate dagster-webserver dagster-daemon
sudo -n /usr/local/sbin/brain-shrik-env-control --start
sudo -n /usr/local/sbin/brain-shrik-env-control --is-active
export BRAIN_CODEX_GATEWAY_UID="$(id -u)"
export BRAIN_CODEX_GATEWAY_GID="$(id -g)"
docker compose up -d --no-deps --force-recreate brain-codex-gateway
docker compose -f "$RED_ROOT/projects/red-codex/docker-compose.local.yml" \
  --env-file "$RED_ROOT/projects/red-codex/.env.local" \
  up -d --no-deps --force-recreate api
```

The second phase rereads exactly the logged generation. It proves once more the new
PostgreSQL credentials accepted, the old ones refused, then runs three bearer probes
from the gateway: absence refused, old refused, new accepted. It deletes the log
only after these proofs:

```bash
uv run python scripts/rotate_codex_gateway_credentials.py \
  --brain-root "$BRAIN_ROOT" \
  --red-root "$RED_ROOT" \
  --private-dir "$ROTATION_DIR" \
  --shrik-env "$SHRIK_ENV" \
  --expected-alembic-revision "$DEPLOYED_HEAD" \
  --apply --resume \
  --consumers-stopped-confirmed \
  --rollback-preflight-confirmed \
  --consumers-recreated-confirmed

systemctl --user start \
  brain-v42-graph-recon.timer brain-v42-embedding-backfill.timer brain-v42-dream.timer
```

If the first phase fails, it automatically attempts the PostgreSQL and file rollback,
keeps the log, and asks for `--resume`. If an error occurs after recreation, quiesce
the consumers again and then explicitly restore the previous generation:

```bash
uv run python scripts/rotate_codex_gateway_credentials.py \
  --brain-root "$BRAIN_ROOT" \
  --red-root "$RED_ROOT" \
  --private-dir "$ROTATION_DIR" \
  --shrik-env "$SHRIK_ENV" \
  --expected-alembic-revision "$DEPLOYED_HEAD" \
  --rollback \
  --consumers-stopped-confirmed \
  --rollback-preflight-confirmed
```

Then recreate only the consumers that were active in the captured state. Never
delete, edit, or copy the log by hand. It contains the private material
strictly required for resume and rollback.

A write role dedicated to the gateway remains an open item. The two trigger functions
run with the caller's rights (`SECURITY INVOKER`, PostgreSQL's default), and
mutations do not yet have a complete RLS contract. A narrow role would fail
on the triggers' reads; giving it broad rights would defeat least privilege.
Do not invent a role or compensating grants in this deployment.

Never `cat` or `echo` the private file. Never copy it into the repository, a log, or
a command recorded in history. `deploy/codex-gateway.env.example` documents
only the variable name; its placeholder deliberately fails startup.
The launcher requires a regular file owned by its UID, the exact mode `0600`, a single
key `BRAIN_CODEX_GATEWAY_TOKEN`, and a bearer of at least 32 bytes.

Compose mounts this file read-only at `/run/secrets/codex-gateway.env`; it does not
load it with `env_file`. It also mounts the killswitch drop-in read-only at
`/run/brain-v42/killswitches.conf`. Verify the killswitch source before startup:

```bash
export BRAIN_DREAM_KILLSWITCHES_FILE="${BRAIN_DREAM_KILLSWITCHES_FILE:-$HOME/.config/systemd/user/brain-v42-dream.service.d/killswitches.conf}"
test -f "$BRAIN_DREAM_KILLSWITCHES_FILE"
test ! -L "$BRAIN_DREAM_KILLSWITCHES_FILE"
```

A file bind-mount keeps the mounted inode. After an atomic replacement of the killswitch
drop-in on the host, force the gateway's recreation, then rerun the `/ready` and
`/api/killswitches` probes; a plain process restart does not prove the new inode is being read.

```bash
export BRAIN_CODEX_GATEWAY_UID="$(id -u)"
export BRAIN_CODEX_GATEWAY_GID="$(id -g)"
docker compose up -d --no-deps --force-recreate brain-codex-gateway
```

Verify that the booleans returned by `/api/killswitches` match the new file.
The same recreation rule applies after an atomic replacement of the bearer file.

## Build and start

The launcher compares the secret's owner to its current UID. Export the UID and GID of
the operator who owns the file before every container creation. The default values
`1001:1001` match the current host; these exports make the deployment portable:

```bash
export BRAIN_CODEX_GATEWAY_UID="$(id -u)"
export BRAIN_CODEX_GATEWAY_GID="$(id -g)"
docker compose config --quiet
docker compose build brain-codex-gateway
docker compose up -d brain-codex-gateway
docker compose ps brain-codex-gateway
```

Wait for the `healthy` state. The Compose healthcheck calls `/ready`, not `/health`. `/health`
only proves the HTTP process responds; it can stay green even if PostgreSQL or the
SQL contract is unavailable. `/ready` requires a PostgreSQL connection, the ten Codex views, the two
migration `036` triggers active for ordinary writes (`ENABLE` or `ENABLE ALWAYS`),
and `security_barrier=true` on the seven scoped views. Inspect
only the state and events; do not display the container's environment.

```bash
test "$(docker inspect --format '{{.State.Health.Status}}' brain_v42_codex_gateway)" = healthy
test "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/run/secrets/codex-gateway.env"}}{{.RW}}{{end}}{{end}}' brain_v42_codex_gateway)" = false
test "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/run/brain-v42/killswitches.conf"}}{{.RW}}{{end}}{{end}}' brain_v42_codex_gateway)" = false
```

The `0.0.0.0:9211` listener stays confined to the container's network namespace: the Compose
service declares neither `ports` nor `expose`. Do not add a host or LAN publish to ease a
test; run the probes from the Docker network.

## Configure `red-codex`

In `red-codex`'s private `.env.local` file, set:

```dotenv
CODEX_BRAIN_DSN=postgresql+asyncpg://codex_ro:<rotated-secret>@brain_v42_postgres:5432/brain
CODEX_BRAIN_GATEWAY_URL=http://brain-codex-gateway:9211
CODEX_BRAIN_GATEWAY_TOKEN=<same value as BRAIN_CODEX_GATEWAY_TOKEN>
```

Keep `.env.local` at mode `0600`, outside Git. Transfer the bearer via a secrets
manager or a masked entry; do not print it to copy it. `red-codex`'s `api`
service must remain attached to `brain_v42_default`, then be recreated to load both
variables:

```bash
cd /path/to/ReD_v1/projects/red-codex
docker compose -f docker-compose.local.yml --env-file .env.local \
  up -d --no-deps --force-recreate api
```

With both variables empty, `red-codex` keeps reads and disables mutations.

## Verify the deployment

### Health and authentication

This probe runs inside the gateway. It checks `/health` liveness, `/ready`
readiness, refusal without a bearer, and authenticated access without writing or displaying the secret:

```bash
docker compose exec -T brain-codex-gateway python - <<'PY'
import json
import urllib.error
import urllib.request
from pathlib import Path

from brain_v42.codex_gateway.launcher import load_gateway_token_file


def status(request: urllib.request.Request) -> int:
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


base = "http://127.0.0.1:9211"
assert status(urllib.request.Request(f"{base}/health")) == 200
assert status(urllib.request.Request(f"{base}/ready")) == 200
assert status(urllib.request.Request(f"{base}/api/killswitches")) == 401
token = load_gateway_token_file(
    Path("/run/secrets/codex-gateway.env")
).get_secret_value()
authenticated = urllib.request.Request(
    f"{base}/api/killswitches",
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(authenticated, timeout=5) as response:
    assert response.status == 200
    killswitches = json.load(response)
expected_keys = {
    "promote_enabled",
    "promote_dry",
    "reorg_enabled",
    "reorg_dry",
    "extract_enabled",
    "extract_dry",
    "roadmap_enabled",
    "roadmap_dry",
}
assert set(killswitches) == expected_keys
assert all(isinstance(value, bool) for value in killswitches.values())
print("gateway health/readiness/auth: OK")
print("killswitches:", json.dumps(killswitches, sort_keys=True))
PY
```

### `red` SQL scope

This read-only probe checks the two root views of the scope. The messages,
artifacts, and proposals views inherit from these roots. The global Dream and consolidation views are
deliberately not part of this check.

```bash
docker compose exec -T brain-codex-gateway python - <<'PY'
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain_v42.config import Settings


SQL = """
SELECT
  (
    SELECT count(*)
    FROM codex_feature_v1 AS feature
    WHERE NOT EXISTS (
      SELECT 1
      FROM project_contexts AS project
      WHERE project.project_group = 'red'
        AND (
          project.project_key = feature.project_key
          OR project.project_key = split_part(feature.project_key, ':', 1)
        )
    )
  ) AS feature_violations,
  (
    SELECT count(*)
    FROM codex_ticket_v1 AS ticket
    WHERE NOT EXISTS (
      SELECT 1
      FROM project_contexts AS project
      WHERE project.project_group = 'red'
        AND (
          project.project_key = ticket.from_project
          OR project.project_key = split_part(ticket.from_project, ':', 1)
        )
    )
      AND NOT EXISTS (
        SELECT 1
        FROM project_contexts AS project
        WHERE project.project_group = 'red'
          AND (
            project.project_key = ticket.to_project
            OR project.project_key = split_part(ticket.to_project, ':', 1)
          )
      )
  ) AS ticket_violations,
  (
    SELECT count(*)
    FROM codex_brain_entity_v1 AS entity
    WHERE entity.type = 'plan'
      AND NOT EXISTS (
        SELECT 1
        FROM project_contexts AS project
        WHERE project.project_group = 'red'
          AND (
            project.project_key = entity.project_key
            OR project.project_key = split_part(entity.project_key, ':', 1)
          )
      )
  ) AS plan_violations
"""


async def main() -> None:
    engine = create_async_engine(Settings().postgres_url)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(SQL))).one()
    finally:
        await engine.dispose()
    result = (
        int(row.feature_violations),
        int(row.ticket_violations),
        int(row.plan_violations),
    )
    assert result == (0, 0, 0), f"red scope violations: {result}"
    print("gateway red SQL scope: OK")


asyncio.run(main())
PY
```

### `red-codex` consumer path

From the `red-codex` host, check the relayed status and then an authenticated read. These
requests do not expose the bearer:

```bash
curl --fail --silent http://127.0.0.1:8091/api/brain/gateway/status \
  | python -c 'import json,sys; assert json.load(sys.stdin) == {"configured": True}'
curl --fail --silent --output /dev/null \
  http://127.0.0.1:8091/api/brain/dream/killswitches
```

The status relayed by `red-codex` probes `/ready`; it therefore only turns positive once the
connection, the views, the barriers, and the triggers are compatible. The direct `/ready` and
the container's `healthy` state nonetheless remain the reference operator proofs.

A full validation requires all three probe groups to be green. Only run a business
mutation with a ticket or proposal explicitly set up for this test.

## Operating contracts and known limits

- A proposal already applied or rejected returns `409`. A source modified since the review
  also returns `409 Proposal state changed; review required`. Reload its state and have it
  reviewed; do not blindly replay the mutation.
- A ticket's `from_project` and `to_project` participants are immutable after creation,
  including for direct SQL access. Create a new ticket if the routing must change.
- The ticket scope guard holds a transaction open for the duration of the call to the canonical service.
  The per-process pool holds 20 connections plus 10 overflow, for a ceiling of 30.
  This version supports a single `red-codex` consumer and bursts strictly below
  30; stay well under this ceiling for ticket mutations. Do not add a gateway
  replica or sustained concurrent load without revisiting the transaction and the sizing.
- The dedicated gateway role remains blocked by the triggers' `SECURITY INVOKER` rights and
  the lack of complete RLS. The rotated `brain` account is therefore an explicit debt of this
  version, not a least-privilege model to copy.

## Rollback

The rollback cuts the consumer first, then the gateway. In `red-codex`'s `.env.local`,
reset `CODEX_BRAIN_GATEWAY_URL` and `CODEX_BRAIN_GATEWAY_TOKEN` to empty, then
recreate its `api` service. The Brain screens remain available read-only.

```bash
cd /path/to/ReD_v1/projects/red-codex
docker compose -f docker-compose.local.yml --env-file .env.local \
  up -d --no-deps --force-recreate api

cd /path/to/brain_v42
docker compose stop brain-codex-gateway
```

Keep the deployed head — measured before the rollback, never copied from a previous run —
during this rollback: its 036 views remain `red-codex`'s read contract.
The gateway rollback authorizes no Alembic migration. Any schema downgrade
falls under the lifecycle and graph runbooks, with a separate operator authorization; never downgrade
just to return to the gateway's previous state.

If the bearer may have leaked, generate a new one, update both private files,
then recreate both services. Never reuse the suspect bearer.
