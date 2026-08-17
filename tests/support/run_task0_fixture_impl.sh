#!/usr/bin/env bash
# Implementation sourced by scripts/run_task0_fixture.sh. Never source this file directly.

task0_prepare() {
  set -euo pipefail
  local shell_pid="$1" shell_pgid="$2" image="$3" container="$4"
  local database="$5" state="$6" created="$7" volumes="$8" port

  printf '%s\n' "$$" > "$shell_pid"
  ps -o pgid= -p "$$" | tr -d '[:space:]' > "$shell_pgid"
  docker info >/dev/null
  docker image inspect "$image" >/dev/null
  : > "$created"
  task0_compose_up "$container" "$database"
  docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' \
    "$container" > "$volumes"
  sed -i '/^[[:space:]]*$/d' "$volumes"
  test ! -s "$volumes"
  docker inspect --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' \
    "$container" > "$state.port"
  IFS= read -r port < "$state.port"
  test -n "$port"
  for _attempt in $(seq 1 25); do
    if task0_database_probe \
      "postgresql://brain:brain@127.0.0.1:${port}/${database}" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  task0_database_probe "postgresql://brain:brain@127.0.0.1:${port}/${database}" \
    | grep -Fx 160014
  printf 'postgresql+asyncpg://brain:brain@127.0.0.1:%s/%s\n' "$port" "$database" > "$state"
}

run_task0_fixture() (
  set -euo pipefail
  local run_id tmpdir container database state volumes created shell_pid shell_pgid fixture_source
  local image='pgvector/pgvector:pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb'

  for command_name in bash docker timeout uuidgen mktemp pgrep ps tr seq sed grep rm sleep tee; do
    command -v "$command_name" >/dev/null 2>&1 || {
      printf '%s\n' 'DB_STATUS=SKIPPED'
      return 0
    }
  done
  test -x .venv/bin/python && .venv/bin/python -c 'import asyncpg' >/dev/null 2>&1 || {
    printf '%s\n' 'DB_STATUS=SKIPPED'
    return 0
  }
  run_id="$(uuidgen | tr '[:upper:]' '[:lower:]')" || {
    printf '%s\n' 'DB_STATUS=SKIPPED'
    return 0
  }
  tmpdir="$(mktemp -d)" || {
    printf '%s\n' 'DB_STATUS=SKIPPED'
    return 0
  }
  container="brain-v42-task0-${run_id}"
  database="brain_v42_${run_id//-/}"
  state="$tmpdir/state"
  volumes="$tmpdir/volumes"
  created="$tmpdir/container-created"
  shell_pid="$tmpdir/shell.pid"
  shell_pgid="$tmpdir/shell.pgid"
  fixture_source="$TASK0_FIXTURE_ENTRYPOINT"

  task0_process_group_is_dead() {
    local pid pgid
    test -s "$shell_pid" && test -s "$shell_pgid" || return 0
    IFS= read -r pid < "$shell_pid"
    IFS= read -r pgid < "$shell_pgid"
    ! kill -0 "$pid" 2>/dev/null && ! pgrep --pgroup "$pgid" >/dev/null 2>&1
  }

  cleanup_pg16() {
    local original_status="$?" cleanup_failed=0 inspect_error
    inspect_error="$tmpdir/inspect.err"
    if test -f "$created" && timeout --foreground -s KILL 2s docker container inspect \
      "$container" >/dev/null 2>"$inspect_error"; then
      if ! timeout --foreground -s KILL 2s docker inspect \
        --format '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' \
        "$container" > "$volumes" 2>>"$inspect_error"; then
        cleanup_failed=1
      fi
      sed -i '/^[[:space:]]*$/d' "$volumes" || cleanup_failed=1
      test ! -s "$volumes" || cleanup_failed=1
      timeout --foreground -s KILL 5s docker rm -v -f "$container" \
        >/dev/null 2>>"$inspect_error" || cleanup_failed=1
    elif test -f "$created" && ! grep -Eq 'No such object|No such container' "$inspect_error"; then
      cleanup_failed=1
    fi
    if timeout --foreground -s KILL 2s docker container inspect "$container" \
      >/dev/null 2>"$inspect_error"; then
      cleanup_failed=1
    elif ! grep -Eq 'No such object|No such container' "$inspect_error"; then
      cleanup_failed=1
    fi
    test ! -s "$volumes" || cleanup_failed=1
    rm -rf -- "$tmpdir" || cleanup_failed=1
    trap - EXIT INT TERM
    if test "$cleanup_failed" -ne 0; then
      printf '%s\n' 'DB_STATUS=CLEANUP_FAILED' >&2
      return 1
    fi
    return "$original_status"
  }
  trap cleanup_pg16 EXIT INT TERM

  if ! timeout -s KILL 30s "$fixture_source" --prepare "$shell_pid" "$shell_pgid" \
    "$image" "$container" "$database" "$state" "$created" "$volumes"; then
    task0_process_group_is_dead || {
      printf '%s\n' 'DB_STATUS=CLEANUP_FAILED' >&2
      return 1
    }
    printf '%s\n' 'DB_STATUS=SKIPPED'
    return 0
  fi
  task0_process_group_is_dead || {
    printf '%s\n' 'DB_STATUS=CLEANUP_FAILED' >&2
    return 1
  }
  export BRAIN_V42_TEST_DB_URL="$(<"$state")"
  printf '%s\n' 'DB_STATUS=READY'
  .venv/bin/python -m pytest -p no:cacheprovider "$@"
)
