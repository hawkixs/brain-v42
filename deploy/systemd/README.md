# Runtime automation systemd — operator runbook

This repo ships `brain-v42-automation.service` **dormant**. The installer renders and
verifies the unit but never enables or starts it. The commands below are
meant for an operator on the target host; their presence in the repo does not mean
a cutover has taken place.

The target topology separates responsibilities:

- `brain-metrics.service` on `127.0.0.1:9200` keeps `/metrics` and `/api/cockpit`;
- `brain-v42-automation.service` on `127.0.0.1:9201` carries `/health`, the GitLab webhook
  and the dedup loop;
- a PostgreSQL advisory lease guarantees at most one automation owner. This lease
  is not a fencing token: work already committed to the database at the moment of a network
  outage cannot be interrupted retroactively;
- the external GitLab hook remains a separate decision. No block repoints it.

`GET :9201/health` only proves the **liveness** of the HTTP process. This signal is
not a PostgreSQL, GPU, reranker or scheduler readiness check. After the split, automation
events are no longer in the in-process aggregate `cockpit.recent`; their authoritative
operational trace moves to the automation unit's journal.

Run each section from the repo root. Only move on to the next one if the
previous one exits with code `0`.

## Render modes

- `install.sh --check-only` renders and verifies all managed units (the list lives in `MANAGED_UNIT_FILES`) in a private directory under
  `/tmp`, then removes it. It neither inspects nor creates the user systemd directory and
  does not call `systemctl`.
- `install.sh --render-dir /new/absolute/path` produces the same verified files in
  a new private target outside systemd. The parent must be owned by the user, have
  `u+wx`, not be group/other writable and contain no symlinked component.
- `install.sh --dry-run` is a legacy mode: it does not call `systemctl`, but **publishes the
  managed units into the user systemd directory**. Do not use it as a side-effect-free
  preflight nor as a global rollout. The live `install` and `--dry-run` paths enforce a
  `077` umask, bring the final directory back to `0700`, publish units as `0600` and refuse
  an owner or an ancestor that would allow replacement by another UID.

Both isolated modes run the preflights with the host HOME/XDG, then a single
`systemd-analyze verify` over all artifacts rendered in an empty, private HOME/XDG. They fail
closed if the verifier is missing or rejects a unit.

On failure after `--render-dir` publication, cleanup first moves the target back into its
private staging and only removes it if the render's identity still matches. A concurrent
replacement is restored or left in a flagged staging for recovery. This defense targets
mistakes and other UIDs; a hostile process sharing the same UID stays within the same
trust boundary and would need a dedicated `dirfd` helper.

## Preflight

This block first verifies all managed units without publishing, produces an artifact inspectable outside
of systemd, backs up the automation fragment and its drop-ins, then publishes **only** the
automation fragment via atomic rename. It then reloads the manager before inspection and
installs a bounded lease probe. The probe shows no sensitive variable: it displays
only `owners` and `waiters`.

Backing up and publishing the fragment are operator mutations. Do not run this
block without an authorized window and a known-good rollback. The Dream, graph and MCP units rendered into
the artifact are not published by this runbook.

### Render path terminology

`RENDER_PARENT` is the private, pre-existing parent directory that contains the rendered
artifacts and any backup directory. `RENDER_DIR` is the new child directory created inside it
for generated unit files; it is not the parent and must not exist before rendering. For example,
`/state/systemd-render.ABC123` is `RENDER_PARENT` and `/state/systemd-render.ABC123/units` is
`RENDER_DIR`. The installer validates the parent ancestry before creating the child.

<!-- runbook:preflight:start -->
```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
EVIDENCE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover"
LEASE_PROBE="${LEASE_PROBE:-$EVIDENCE_DIR/lease-probe.py}"
PROC_ROOT="${PROC_ROOT:-/proc}"
LOCK_KEY=4151019227643017711
mkdir -p "$EVIDENCE_DIR"
chmod 0700 "$EVIDENCE_DIR"
RENDER_PARENT="${RENDER_PARENT:-$(mktemp -d "$EVIDENCE_DIR/systemd-render.XXXXXX")}"
RENDER_DIR="${RENDER_DIR:-$RENDER_PARENT/units}"
BACKUP_DIR="${BACKUP_DIR:-$RENDER_PARENT/live-backup}"
readonly REPO_ROOT USER_UNIT_DIR EVIDENCE_DIR LEASE_PROBE RENDER_PARENT RENDER_DIR \
  BACKUP_DIR PROC_ROOT LOCK_KEY
mkdir -p "$RENDER_PARENT" "$BACKUP_DIR"
chmod 0700 "$RENDER_PARENT" "$BACKUP_DIR"
test ! -e "$RENDER_DIR" && test ! -L "$RENDER_DIR"

NEW_UNIT=""
cleanup_new_unit() {
  if [[ -n "$NEW_UNIT" && ( -e "$NEW_UNIT" || -L "$NEW_UNIT" ) ]]; then
    rm -f -- "$NEW_UNIT"
  fi
}
trap cleanup_new_unit EXIT

mkdir -p "$(dirname "$LEASE_PROBE")"
{
  printf '#!%s/.venv/bin/python\n' "$REPO_ROOT"
  cat <<'PYTHON'
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain_v42.config import Settings

SQL = """
SELECT
    count(*) FILTER (WHERE granted) AS owners,
    count(*) FILTER (WHERE NOT granted) AS waiters
FROM pg_locks
WHERE locktype = 'advisory'
  AND classid::bigint = 966484478::bigint
  AND objid::bigint = 2541386223::bigint
  AND objsubid = 1
  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND mode = 'ExclusiveLock'
"""


async def main() -> None:
    expected = int(os.environ["EXPECTED_AUTOMATION_LEASES"])
    engine = create_async_engine(Settings().postgres_url)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(SQL))).one()
    finally:
        await engine.dispose()
    owners = int(row.owners)
    waiters = int(row.waiters)
    print(f"owners={owners} waiters={waiters}")
    if owners != expected or waiters != 0:
        raise SystemExit(
            f"expected owners={expected} waiters=0, got owners={owners} waiters={waiters}"
        )


asyncio.run(main())
PYTHON
} > "$LEASE_PROBE"
chmod 0700 "$LEASE_PROBE"

"$REPO_ROOT/deploy/systemd/install.sh" --check-only
"$REPO_ROOT/deploy/systemd/install.sh" --render-dir "$RENDER_DIR"
test -f "$RENDER_DIR/brain-v42-automation.service"
if [[ -e "$USER_UNIT_DIR/brain-v42-automation.service" \
  || -L "$USER_UNIT_DIR/brain-v42-automation.service" ]]; then
  cp -a -- "$USER_UNIT_DIR/brain-v42-automation.service" "$BACKUP_DIR/"
fi
if [[ -d "$USER_UNIT_DIR/brain-v42-automation.service.d" ]]; then
  cp -a -- "$USER_UNIT_DIR/brain-v42-automation.service.d" "$BACKUP_DIR/"
fi
mkdir -p "$USER_UNIT_DIR"
NEW_UNIT="$(mktemp "$USER_UNIT_DIR/.brain-v42-automation.service.new.XXXXXX")"
install -m 0644 "$RENDER_DIR/brain-v42-automation.service" "$NEW_UNIT"
cmp -s "$RENDER_DIR/brain-v42-automation.service" "$NEW_UNIT"
mv -f -- "$NEW_UNIT" "$USER_UNIT_DIR/brain-v42-automation.service"
NEW_UNIT=""
systemctl --user daemon-reload
systemctl --user show-environment >/dev/null
test -f "$USER_UNIT_DIR/brain-v42-automation.service"
systemd-analyze --user verify "$USER_UNIT_DIR/brain-v42-automation.service"
systemctl --user show brain-metrics.service -p EnvironmentFiles --value
systemctl --user show brain-metrics.service \
  -p ActiveState -p SubState -p MainPID -p UnitFileState \
  -p FragmentPath -p DropInPaths \
  -p Requires -p Wants -p PartOf -p BindsTo -p Conflicts
systemctl --user show brain-v42-automation.service \
  -p ActiveState -p SubState -p MainPID -p UnitFileState \
  -p FragmentPath -p DropInPaths \
  -p Requires -p Wants -p PartOf -p BindsTo -p Conflicts
MAIN_PID="$(systemctl --user show brain-metrics.service -p MainPID --value)"
[[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]] || {
  printf 'ERROR: brain-metrics has no running MainPID\n' >&2
  exit 1
}
effective_legacy_flag="$(tr '\0' '\n' < "$PROC_ROOT/$MAIN_PID/environ" \
  | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true)"
printf '%s\n' "$effective_legacy_flag"
[[ "$effective_legacy_flag" == 'METRICS_LEGACY_AUTOMATION_ENABLED=true' ]] || {
  printf 'ERROR: expected METRICS_LEGACY_AUTOMATION_ENABLED=true before cutover\n' >&2
  exit 1
}
# Expected before cutover: owners=1 waiters=0.
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
command -v ss >/dev/null || {
  printf 'ERROR: ss is required to verify TCP port 9201\n' >&2
  exit 1
}
tcp_9201_listeners="$(ss -H -ltn 'sport = :9201' 2>&1)"
if [[ -n "$tcp_9201_listeners" ]]; then
  printf 'ERROR: TCP port 9201 is already bound or could not be inspected\n' >&2
  exit 1
fi
preflight_9201="$({
  curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 http://127.0.0.1:9201/health
} || true)"
printf 'preflight automation health status=%s\n' "${preflight_9201:-000}"
case "${preflight_9201:-000}" in
  000) ;;
  *) printf 'ERROR: automation port 9201 is already bound (HTTP %s)\n' \
       "$preflight_9201" >&2; exit 1 ;;
esac
```
<!-- runbook:preflight:end -->

The expected result before cutover is the effective legacy flag `true`, read without displaying
other process variables, then a lease `owners=1 waiters=0` held by metrics.
TCP port `9201` must be free even if no HTTP server responds. Any other result
forbids continuing.

## Cutover

The drop-in carries a second `EnvironmentFile=` loaded after the metrics unit's `.env`.
The dedicated file is private (`0600`) and becomes the authoritative source for the flag. We stop
metrics before starting automation: no dual-run is tolerated.

<!-- runbook:cutover:start -->
```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
OWNER_ENV="$HOME/.config/brain-v42/automation-owner.env"
DROPIN_DIR="$USER_UNIT_DIR/brain-metrics.service.d"
DROPIN="$DROPIN_DIR/90-automation-owner.conf"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"
PROC_ROOT="${PROC_ROOT:-/proc}"

assert_http_status() {
  local expected="$1" method="$2" url="$3" actual
  actual="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 -X "$method" "$url")"
  [[ "$actual" == "$expected" ]] || {
    printf 'HTTP %s %s: expected %s, got %s\n' "$method" "$url" "$expected" "$actual" >&2
    return 1
  }
}

assert_process_flag() {
  local unit="$1" expected="$2" effective
  MAIN_PID="$(systemctl --user show "$unit" -p MainPID --value)"
  [[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]]
  effective="$(tr '\0' '\n' < "$PROC_ROOT/$MAIN_PID/environ" \
    | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true)"
  [[ "$effective" == "METRICS_LEGACY_AUTOMATION_ENABLED=$expected" ]]
}

umask 077
mkdir -p "$(dirname "$OWNER_ENV")" "$DROPIN_DIR"
printf '%s\n' 'METRICS_LEGACY_AUTOMATION_ENABLED=false' > "$OWNER_ENV"
chmod 0600 "$OWNER_ENV"
cat > "$DROPIN" <<'SYSTEMD'
[Service]
EnvironmentFile=%h/.config/brain-v42/automation-owner.env
SYSTEMD
chmod 0644 "$DROPIN"
systemctl --user daemon-reload

environment_files="$(
  systemctl --user show brain-metrics.service -p EnvironmentFiles --value
)"
case "$environment_files" in
  *"$OWNER_ENV (ignore_errors=no)") ;;
  *) printf 'late EnvironmentFile is not last: %s\n' "$environment_files" >&2; exit 1 ;;
esac

systemctl --user stop brain-metrics.service
EXPECTED_AUTOMATION_LEASES=0 "$LEASE_PROBE"
systemctl --user start brain-v42-automation.service
assert_http_status 200 GET http://127.0.0.1:9201/health
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
systemctl --user start brain-metrics.service
assert_process_flag brain-metrics.service false
assert_http_status 200 GET http://127.0.0.1:9200/metrics
assert_http_status 404 POST http://127.0.0.1:9200/gitlab/webhook
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```
<!-- runbook:cutover:end -->

The authoritative proof of the flag is the filtered read of
`/proc/$MainPID/environ` after startup. `systemctl show -p Environment` is not enough:
it can display a declared configuration without proving the process's actual environment.

## Immediate abort

Run this block at the first cutover failure. The `owners=0 waiters=0` check strictly precedes
writing the `true` flag and restarting metrics. With `set -e`, a lease
still held stops the block before these mutations.

<!-- runbook:abort:start -->
```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
OWNER_ENV="$HOME/.config/brain-v42/automation-owner.env"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"
PROC_ROOT="${PROC_ROOT:-/proc}"

assert_http_status() {
  local expected="$1" method="$2" url="$3" actual
  actual="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 -X "$method" "$url")"
  [[ "$actual" == "$expected" ]]
}

assert_process_flag() {
  local unit="$1" expected="$2" effective
  MAIN_PID="$(systemctl --user show "$unit" -p MainPID --value)"
  [[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]]
  effective="$(tr '\0' '\n' < "$PROC_ROOT/$MAIN_PID/environ" \
    | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true)"
  [[ "$effective" == "METRICS_LEGACY_AUTOMATION_ENABLED=$expected" ]]
}

systemctl --user stop brain-v42-automation.service
systemctl --user reset-failed brain-v42-automation.service
EXPECTED_AUTOMATION_LEASES=0 "$LEASE_PROBE"
umask 077
mkdir -p "$(dirname "$OWNER_ENV")"
printf '%s\n' 'METRICS_LEGACY_AUTOMATION_ENABLED=true' > "$OWNER_ENV"
chmod 0600 "$OWNER_ENV"
systemctl --user daemon-reload
systemctl --user restart brain-metrics.service
assert_process_flag brain-metrics.service true
assert_http_status 200 GET http://127.0.0.1:9200/metrics
assert_http_status 401 POST http://127.0.0.1:9200/gitlab/webhook
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```
<!-- runbook:abort:end -->

## Smoke tests

This block is a fail-fast matrix. It proves the reduced surface of `:9201`, the
metrics surface intact on `:9200`, the disappearance of the legacy webhook and the uniqueness of the lease.

<!-- runbook:smoke:start -->
```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"

assert_http_status() {
  local expected="$1" method="$2" url="$3" actual
  actual="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 -X "$method" "$url")"
  [[ "$actual" == "$expected" ]] || {
    printf 'HTTP %s %s: expected %s, got %s\n' "$method" "$url" "$expected" "$actual" >&2
    return 1
  }
}

assert_http_status 200 GET http://127.0.0.1:9201/health
assert_http_status 404 GET http://127.0.0.1:9201/metrics
assert_http_status 404 GET http://127.0.0.1:9201/api/cockpit
assert_http_status 401 POST http://127.0.0.1:9201/gitlab/webhook
assert_http_status 200 GET http://127.0.0.1:9200/metrics
assert_http_status 200 GET http://127.0.0.1:9200/api/cockpit
assert_http_status 404 POST http://127.0.0.1:9200/gitlab/webhook
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```
<!-- runbook:smoke:end -->

Only repoint the GitLab hook to `:9201` after a separate operator decision. Only run
`systemctl --user enable brain-v42-automation.service` after the agreed soak.

## Diagnostics

A red bind is diagnosed without starting a second instance. A **lease conflict** at
startup means an owner is still active: never force nor bypass the
lease. An authenticated webhook that answers `503` with `ownership_lost` confirms the fail-closed
loss of ownership; consult the journal before any restart decision.

```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"
ss -ltnp 'sport = :9201'
systemctl --user status brain-v42-automation.service --no-pager
journalctl --user -u brain-v42-automation.service --since '-30 min' --no-pager
curl --silent --show-error --connect-timeout 2 --max-time 5 \
  http://127.0.0.1:9201/health
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```

The journal output replaces the lost in-process visibility of `cockpit.recent` for
automation events; `/metrics` and `/api/cockpit` remain served by metrics on `:9200`.

## Rollback

Mandatory external prerequisite: disable the GitLab hook outside this repo, without
repointing it. Export `HOOK_DISABLED_CONFIRMED=yes` only after confirmation. The rollback
only re-enables the hook after a full green and a new, separate decision.

<!-- runbook:rollback:start -->
```bash
set -euo pipefail
if [[ "${HOOK_DISABLED_CONFIRMED:-}" != "yes" ]]; then
  printf 'ERROR: HOOK_DISABLED_CONFIRMED must equal yes before rollback\n' >&2
  exit 1
fi
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
OWNER_ENV="$HOME/.config/brain-v42/automation-owner.env"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"
PROC_ROOT="${PROC_ROOT:-/proc}"

assert_http_status() {
  local expected="$1" method="$2" url="$3" actual
  actual="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 -X "$method" "$url")"
  [[ "$actual" == "$expected" ]]
}

assert_process_flag() {
  local unit="$1" expected="$2" effective
  MAIN_PID="$(systemctl --user show "$unit" -p MainPID --value)"
  [[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]]
  effective="$(tr '\0' '\n' < "$PROC_ROOT/$MAIN_PID/environ" \
    | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true)"
  [[ "$effective" == "METRICS_LEGACY_AUTOMATION_ENABLED=$expected" ]]
}

systemctl --user stop brain-v42-automation.service
systemctl --user disable brain-v42-automation.service
EXPECTED_AUTOMATION_LEASES=0 "$LEASE_PROBE"
umask 077
mkdir -p "$(dirname "$OWNER_ENV")"
printf '%s\n' 'METRICS_LEGACY_AUTOMATION_ENABLED=true' > "$OWNER_ENV"
chmod 0600 "$OWNER_ENV"
systemctl --user daemon-reload
systemctl --user restart brain-metrics.service
assert_process_flag brain-metrics.service true
assert_http_status 200 GET http://127.0.0.1:9200/metrics
assert_http_status 401 POST http://127.0.0.1:9200/gitlab/webhook
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```
<!-- runbook:rollback:end -->

Keep the template, the drop-in and the environment file for the whole soak. Their
removal is a later cleanup operation, never a step of the urgent rollback.
