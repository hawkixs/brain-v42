#!/usr/bin/env bash
# Public entry point for the disposable PostgreSQL 16.14 test fixture.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASK0_FIXTURE_ENTRYPOINT="$SCRIPT_DIR/run_task0_fixture.sh"

task0_compose_up() {
  local container="$1" database="$2"
  TASK0_CONTAINER="$container" TASK0_DATABASE="$database" \
    docker compose -f "$SCRIPT_DIR/../tests/support/task0-compose.yml" up --detach --no-build \
    >/dev/null
}

task0_database_probe() {
  local dsn="$1"
  PG16_DSN="$dsn" timeout --foreground -s KILL 2s .venv/bin/python \
    "$SCRIPT_DIR/../tests/support/task0_probe.py"
}

source "$SCRIPT_DIR/../tests/support/run_task0_fixture_impl.sh"

if [[ "${BASH_SOURCE[0]}" == "$0" && "${1:-}" == "--prepare" ]]; then
  shift
  task0_prepare "$@"
fi
