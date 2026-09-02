# MCP HTTP systemd — operator runbook

This runbook covers the shared service `brain-mcp-http.service` and its watchdog. The normal
installation path generates and validates the units without changing their state. Starting
them, enabling them at boot, and stopping them remain operator decisions.

The production systemd contract is fixed to `127.0.0.1:8765`, like the versioned `.mcp.json`
client.
An override of `Settings.mcp_http_port` remains possible for development outside this path,
but the installer and the production service refuse any other value so that server,
clients, healthchecks, and watchdog cannot diverge.

> **Destructive scope of the uninstall.** `deploy/systemd/install.sh --uninstall`
> stops, disables, and removes **all** units managed by the script: MCP HTTP,
> watchdog, Dream, graph-recon, and automation. Do not use it as a simple MCP rollback.
> The combination `--dry-run --uninstall` is rejected with no side effect.

The commands below run from the root of the checkout intended for production.
Never copy a secret value into a log or a shared piece of evidence.

### Render path terminology

`render_parent` is the private, pre-existing parent directory that contains the rendered
artifacts and any backup directory. `render_dir` is the new child directory created inside it
for the generated unit files; it is not the parent and it must not exist before rendering.
For example, `/state/systemd-render.ABC123` is `render_parent` and
`/state/systemd-render.ABC123/units` is `render_dir`. The installer checks the parent ancestry
for canonical, user-owned, non-replaceable path components before creating the child.

## Upgrading an existing unit

Older installations could keep `MCP_HTTP_TOKEN` in the checkout's `.env`.
The new preflight blocks **before any regeneration** as long as this duplication exists:
the next restart therefore cannot silently switch to an invalid configuration.

Before `install.sh`, check the situation without printing the value:

```bash
set -euo pipefail
repo_root="$(pwd)"
private_token="$HOME/.config/brain-v42/mcp-token.env"
test -f "$private_token"
grep -Eq '^[[:space:]]*MCP_HTTP_TOKEN=.+$' "$private_token"
if grep -Eqi '^[[:space:]]*(export[[:space:]]+)?MCP_HTTP_(TOKEN|DREAM_TOKENS)[[:space:]]*=' \
  "$repo_root/.env"; then
  echo 'MIGRATION REQUIRED: remove private MCP keys from the shared .env' >&2
  exit 2
fi
```

If the gate signals a migration, back up `.env` to a private `0700/0600` location,
privately confirm that `mcp-token.env` carries the current bearer, then remove **only** the
`MCP_HTTP_TOKEN`/`MCP_HTTP_DREAM_TOKENS` assignments from `.env` with a local editor. Do not
put the value in shell history, a diff, or a ticket. Then rerun the block above
and the full preflight. This repository neither migrates nor automatically removes the host's secret.

## Preflight

First check the checkout, the private files, and the eight units without touching the
live systemd directory, with `--check-only`. `--render-dir` then produces a private artifact
outside of systemd. The block backs up and publishes only the three MCP fragments after
explicit neutralization of the watchdog, then reloads the manager. This neutralization is the
first lifecycle mutation; the watchdog will only be reactivated after the go/no-go.

Do not replace this sequence with `--dry-run`: this legacy mode writes the eight managed
units to `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`, even though it does not call `systemctl`.

```bash
set -euo pipefail
repo_root="$(pwd)"
user_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
evidence_root="${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-mcp-upgrade"
mkdir -p "$evidence_root"
chmod 0700 "$evidence_root"
render_parent="$(mktemp -d "$evidence_root/systemd-render.XXXXXX")"
render_dir="$render_parent/units"
backup_dir="$render_parent/live-backup"
mkdir -p "$backup_dir"
chmod 0700 "$render_parent" "$backup_dir"
test -x "$repo_root/.venv/bin/python"

"$repo_root/.venv/bin/python" "$repo_root/scripts/check_graph_projector_env.py" \
  --shared "$repo_root/.env" \
  --private "$HOME/.config/brain-v42/graph-projector.env"
"$repo_root/.venv/bin/python" "$repo_root/scripts/check_mcp_http_port.py" \
  --shared "$repo_root/.env" --expected 8765 --expected-host 127.0.0.1 \
  --token-file "$HOME/.config/brain-v42/mcp-token.env"

deploy/systemd/install.sh --check-only
deploy/systemd/install.sh --render-dir "$render_dir"
for unit in \
  brain-mcp-http.service \
  brain-mcp-http-watchdog.service \
  brain-mcp-http-watchdog.timer; do
  test -f "$render_dir/$unit"
done
systemctl --user stop brain-mcp-http-watchdog.timer
systemctl --user stop brain-mcp-http-watchdog.service
systemctl --user disable --no-reload brain-mcp-http-watchdog.timer
mkdir -p "$user_unit_dir"
for dropin_dir in \
  brain-mcp-http.service.d \
  brain-mcp-http-watchdog.service.d \
  brain-mcp-http-watchdog.timer.d; do
  if [[ -d "$user_unit_dir/$dropin_dir" ]]; then
    cp -a -- "$user_unit_dir/$dropin_dir" "$backup_dir/"
  fi
done
new_unit=""
cleanup_new_unit() {
  if [[ -n "$new_unit" && ( -e "$new_unit" || -L "$new_unit" ) ]]; then
    rm -f -- "$new_unit"
  fi
}
trap cleanup_new_unit EXIT
for unit in \
  brain-mcp-http.service \
  brain-mcp-http-watchdog.service \
  brain-mcp-http-watchdog.timer; do
  if [[ -e "$user_unit_dir/$unit" || -L "$user_unit_dir/$unit" ]]; then
    cp -a -- "$user_unit_dir/$unit" "$backup_dir/"
  fi
  new_unit="$(mktemp "$user_unit_dir/.$unit.new.XXXXXX")"
  install -m 0644 "$render_dir/$unit" "$new_unit"
  cmp -s "$render_dir/$unit" "$new_unit"
  mv -f -- "$new_unit" "$user_unit_dir/$unit"
  new_unit=""
done
systemctl --user daemon-reload
systemd-analyze --user verify \
  "$user_unit_dir/brain-mcp-http.service" \
  "$user_unit_dir/brain-mcp-http-watchdog.service" \
  "$user_unit_dir/brain-mcp-http-watchdog.timer"
systemctl --user show brain-mcp-http.service \
  -p LoadState -p ActiveState -p SubState -p UnitFileState -p FragmentPath -p DropInPaths
systemctl --user show brain-mcp-http-watchdog.timer \
  -p LoadState -p ActiveState -p SubState -p UnitFileState -p FragmentPath -p DropInPaths
```

### Preflight of files outside systemd

This preflight is executable from the operator shell: it attests only the checkout, the
shared flag, and the private file. It does not load, source, or print any private value. Do
not add `--require-effective-private` to it: that option requires the environment ultimately
built by systemd, which is absent from the operator shell.

```bash
set -euo pipefail
repo_root="$(pwd)"
"$repo_root/.venv/bin/python" "$repo_root/scripts/check_graph_projector_env.py" \
  --shared "$repo_root/.env" \
  --private "$HOME/.config/brain-v42/graph-projector.env"
```

The MCP preflight attests `.env` and `mcp-token.env` via `lstat`: regular files with no
symlink, owner identical to the service, mode `0600`, bounded size, and exactly one non-empty
`MCP_HTTP_TOKEN`. At systemd startup, it also compares the effective values of the admin
bearer, the Dream registry, and its activation flag without ever printing a secret. The private
file is reserved for `MCP_HTTP_TOKEN`, `MCP_HTTP_DREAM_TOKENS`, and
`BRAIN_DREAM_CAPABILITY_ENFORCEMENT`; any other assignment, concurrent casing, or `export`
syntax is rejected. The projector preflight accepts the absence of the private file only if
`GRAPH_LEDGER_WRITE_ENABLED` is effectively off. With the ledger active, it requires a regular
file, not a symlink, owned by the service user, in mode `0600`, with exactly the four expected
`GRAPH_PROJECTOR_*` keys and a non-placeholder password.

Before the canary, the client process must receive the same bearer via its secrets
manager: `.mcp.json` expands `${MCP_HTTP_TOKEN}` in the `Authorization` header. Do not
reintroduce this secret into `.env` and do not `source` the systemd file from Bash: the two
grammars differ. From the private environment that will launch or relaunch the client, verify
the match without printing the value:

```bash
set -euo pipefail
repo_root="$(pwd)"
test -n "${MCP_HTTP_TOKEN:-}"
"$repo_root/.venv/bin/python" "$repo_root/scripts/check_mcp_http_port.py" \
  --shared "$repo_root/.env" --expected 8765 --expected-host 127.0.0.1 \
  --token-file "$HOME/.config/brain-v42/mcp-token.env" \
  --require-effective-token
```

## First activation or host migration

Keep the watchdog disabled during the canary: its role is to restart the service on a
`/health` failure, which would mask a repeated configuration defect.

```bash
set -euo pipefail
systemctl --user stop brain-mcp-http-watchdog.timer
systemctl --user stop brain-mcp-http-watchdog.service
systemctl --user disable --no-reload brain-mcp-http-watchdog.timer
```

### Effective attestation by systemd

After `daemon-reload`, the restart is the effective attestation: systemd loads its
`EnvironmentFile` then runs the `ExecStartPre` that keeps
`--require-effective-private`. Do not reproduce this check in the shell and never
`source` the private file; a failure here blocks startup without printing secrets.

```bash
set -euo pipefail
old_pid="$(systemctl --user show brain-mcp-http.service -p MainPID --value || true)"
systemctl --user restart brain-mcp-http.service
systemctl --user is-active --quiet brain-mcp-http.service
new_pid="$(systemctl --user show brain-mcp-http.service -p MainPID --value)"
test "$new_pid" -gt 0
if [[ -n "$old_pid" && "$old_pid" != 0 ]]; then
  test "$new_pid" != "$old_pid"
fi
```

Immediately continue with the canary:

```bash
set -euo pipefail
healthy=false
for attempt in {1..30}; do
  if curl -fsS -m 2 http://127.0.0.1:8765/health; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  journalctl --user -u brain-mcp-http.service --since '-10 min' --no-pager
  exit 1
fi
journalctl --user -u brain-mcp-http.service --since '-10 min' --no-pager
```

After the healthcheck, run a **read-only** Brain call from a production client
and verify its identity in the metrics. For a host migration, keep the old host
available until this proof and switch clients over one by one. Do not enable the watchdog until
calls and logs are stable.

The operator go/no-go then makes the server and the watchdog persistent:

```bash
systemctl --user enable brain-mcp-http.service
systemctl --user enable --now brain-mcp-http-watchdog.timer
```

User linger is a separate prerequisite for surviving a disconnection. Check it
with `loginctl show-user "$(id -u)" -p Linger`; any linger change requires host
administrator rights and is not performed by the installer.

## Validation

```bash
set -euo pipefail
systemctl --user is-enabled brain-mcp-http.service
systemctl --user is-active brain-mcp-http.service
systemctl --user is-enabled brain-mcp-http-watchdog.timer
systemctl --user is-active brain-mcp-http-watchdog.timer
curl -fsS -m 10 http://127.0.0.1:8765/health
systemctl --user list-timers brain-mcp-http-watchdog.timer --no-pager
journalctl --user -u brain-mcp-http.service \
  -u brain-mcp-http-watchdog.service --since '-30 min' --no-pager
```

`/health` is exempt from authentication and proves server liveness as well as a bounded
PostgreSQL checkout. It replaces neither a real MCP call, nor GPU/reranker checks, nor
proof of client scope.

## Rollback

Always neutralize the timer before the server, then keep the private files for
off-log diagnostics.

```bash
set -euo pipefail
systemctl --user stop brain-mcp-http-watchdog.timer
systemctl --user stop brain-mcp-http-watchdog.service
systemctl --user disable --no-reload brain-mcp-http-watchdog.timer
systemctl --user disable --now brain-mcp-http.service
```

On a version regression, run `--check-only` from a known-good checkout, produce
a new `--render-dir`, then restore only the three MCP basenames from
`$backup_dir` created at preflight, after neutralizing the watchdog. Multi-file
atomicity does not exist: the guarantee provided below is compensatory and fail-closed. The three
replacements and the three snapshots of the current state are prepared before any mutation; if a
replacement fails, the units already replaced are restored in reverse order and the command
fails non-zero. If the compensation also fails, it leaves the snapshots in the staging
directory for manual recovery and fails non-zero, without proceeding to `daemon-reload`.

### Compensatory rollback of units

This command runs in the same shell as the preflight. It fails before mutation if a
backup or a current unit is missing. It touches neither the `EnvironmentFile`, nor the
private files, nor the Neo4j credentials. After success, reload the manager then resume the
file preflight, the systemd attestation, and the canary.

```bash
set -euo pipefail
rollback_units=(
  brain-mcp-http.service \
  brain-mcp-http-watchdog.service \
  brain-mcp-http-watchdog.timer
)
rollback_stage_dir="$(mktemp -d "$user_unit_dir/.brain-mcp-http-rollback.XXXXXX")"
chmod 0700 "$rollback_stage_dir"
rollback_committed=()

rollback_compensate() {
  local index unit compensation_unit
  for ((index=${#rollback_committed[@]} - 1; index>=0; index--)); do
    unit="${rollback_committed[index]}"
    compensation_unit="$(mktemp "$user_unit_dir/.$unit.compensate.XXXXXX")"
    install -m 0644 "$rollback_stage_dir/original.$unit" "$compensation_unit" || return 1
    cmp -s "$rollback_stage_dir/original.$unit" "$compensation_unit" || return 1
    mv -f -- "$compensation_unit" "$user_unit_dir/$unit" || return 1
  done
}

rollback_failed() {
  local status="$1"
  trap - ERR
  if ! rollback_compensate; then
    echo 'ERROR: rollback compensation failed; retain the stage directory for manual recovery' >&2
  fi
  exit "$status"
}

trap 'rollback_failed $?' ERR
for unit in "${rollback_units[@]}"; do
  test -f "$backup_dir/$unit"
  test -f "$user_unit_dir/$unit"
  install -m 0644 "$backup_dir/$unit" "$rollback_stage_dir/replacement.$unit"
  cmp -s "$backup_dir/$unit" "$rollback_stage_dir/replacement.$unit"
  install -m 0644 "$user_unit_dir/$unit" "$rollback_stage_dir/original.$unit"
  cmp -s "$user_unit_dir/$unit" "$rollback_stage_dir/original.$unit"
done
for unit in "${rollback_units[@]}"; do
  mv -f -- "$rollback_stage_dir/replacement.$unit" "$user_unit_dir/$unit"
  rollback_committed+=("$unit")
done
systemctl --user daemon-reload
trap - ERR
rm -rf -- "$rollback_stage_dir"
```

On a migration, repoint clients to the old host only if it is still validated.

## Full uninstall

This command is appropriate only if the operator wants to remove the entire systemd stack
managed by this script. It first stops the timers/watchdogs, disables the services, removes their
units, and reloads the manager:

```bash
deploy/systemd/install.sh --uninstall
```

After execution, configuration files and secrets are not removed. The managed
units, however, must all be absent from
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`.
